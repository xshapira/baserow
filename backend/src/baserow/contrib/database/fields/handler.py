import logging
from copy import deepcopy
from typing import (
    Dict,
    Any,
    Optional,
    List,
    TypeVar,
    Type,
    cast,
    Union,
    Tuple,
    Callable,
)
from psycopg2 import sql

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import connection
from django.db.models import QuerySet
from django.db.utils import ProgrammingError, DataError


from baserow.contrib.database.db.schema import (
    lenient_schema_editor,
    safe_django_schema_editor,
)
from baserow.contrib.database.fields.constants import (
    RESERVED_BASEROW_FIELD_NAMES,
    UPSERT_OPTION_DICT_KEY,
)
from baserow.contrib.database.fields.models import TextField
from baserow.contrib.database.fields.field_converters import (
    MultipleSelectConversionConfig,
)
from baserow.contrib.database.table.models import Table
from baserow.contrib.database.views.handler import ViewHandler
from baserow.contrib.database.db.sql_queries import (
    sql_drop_try_cast,
    sql_create_try_cast,
)
from baserow.core.trash.exceptions import RelatedTableTrashedException
from baserow.core.trash.handler import TrashHandler
from baserow.core.utils import extract_allowed, set_allowed_attrs
from .dependencies.handler import FieldDependencyHandler
from .dependencies.update_collector import CachingFieldUpdateCollector
from .exceptions import (
    PrimaryFieldAlreadyExists,
    CannotDeletePrimaryField,
    CannotChangeFieldType,
    FieldDoesNotExist,
    IncompatiblePrimaryFieldTypeError,
    MaxFieldLimitExceeded,
    FieldWithSameNameAlreadyExists,
    ReservedBaserowFieldNameException,
    InvalidBaserowFieldName,
    MaxFieldNameLengthExceeded,
    IncompatibleFieldTypeForUniqueValues,
)
from .models import Field, SelectOption, SpecificFieldForUpdate
from .registries import (
    field_type_registry,
    field_converter_registry,
)
from .signals import (
    field_created,
    field_updated,
    field_deleted,
    field_restored,
    before_field_deleted,
)

logger = logging.getLogger(__name__)


def _validate_field_name(
    field_values: Dict[str, Any],
    table: Table,
    existing_field: Optional[Field] = None,
    raise_if_name_missing: bool = True,
):
    """
    Raises various exceptions if the provided field name is invalid.

    :param field_values: The dictionary which should contain a name key.
    :param table: The table to check that this field name is valid for.
    :param existing_field: If this is name change for an existing field then the
        existing field instance must be provided here.
    :param raise_if_name_missing: When True raises a InvalidBaserowFieldName if the
        name key is not in field_values. When False does not return and immediately
        returns if the key is missing.
    :raises InvalidBaserowFieldName: If "name" is
    :raises MaxFieldNameLengthExceeded: When a provided field name is too long.
    :return:
    """

    if "name" not in field_values:
        if raise_if_name_missing:
            raise InvalidBaserowFieldName()
        else:
            return

    name = field_values["name"]
    if existing_field is not None and existing_field.name == name:
        return

    max_field_name_length = Field.get_max_name_length()
    if len(name) > max_field_name_length:
        raise MaxFieldNameLengthExceeded(max_field_name_length)

    if name.strip() == "":
        raise InvalidBaserowFieldName()

    if Field.objects.filter(table=table, name=name).exists():
        raise FieldWithSameNameAlreadyExists(
            f"A field already exists for table '{table.name}' with the name '{name}'."
        )

    if name in RESERVED_BASEROW_FIELD_NAMES:
        raise ReservedBaserowFieldNameException(
            f"A field named {name} cannot be created as it already exists as a "
            f"reserved Baserow field name."
        )


T = TypeVar("T", bound="Field")


class FieldHandler:
    def get_field(
        self,
        field_id: int,
        field_model: Optional[Type[T]] = None,
        base_queryset: Optional[QuerySet] = None,
    ) -> T:
        """
        Selects a field with a given id from the database.

        :param field_id: The identifier of the field that must be returned.
        :type field_id: int
        :param field_model: If provided that model's objects are used to select the
            field. This can for example be useful when you want to select a TextField or
            other child of the Field model.
        :type field_model: Type[Field]
        :param base_queryset: The base queryset from where to select the field.
            object. This can for example be used to do a `select_related`. Note that
            if this is used the `field_model` parameter doesn't work anymore.
        :type base_queryset: Queryset
        :raises FieldDoesNotExist: When the field with the provided id does not exist.
        :return: The requested field instance of the provided id.
        :rtype: Field
        """

        if not field_model:
            field_model = Field

        if base_queryset is None:
            base_queryset = field_model.objects

        try:
            field = base_queryset.select_related("table__database__group").get(
                id=field_id
            )
        except Field.DoesNotExist:
            raise FieldDoesNotExist(f"The field with id {field_id} does not exist.")

        if TrashHandler.item_has_a_trashed_parent(field.table, check_item_also=True):
            raise FieldDoesNotExist(f"The field with id {field_id} does not exist.")

        return field

    def get_specific_field_for_update(
        self, field_id: int, field_model: Optional[Type[T]] = None
    ) -> SpecificFieldForUpdate:
        return cast(
            SpecificFieldForUpdate,
            self.get_field(
                field_id,
                field_model,
                base_queryset=Field.objects.select_for_update(of=("self",)),
            ).specific,
        )

    def create_field(
        self,
        user: AbstractUser,
        table: Table,
        type_name: str,
        primary=False,
        do_schema_change=True,
        return_updated_fields=False,
        primary_key=None,
        **kwargs,
    ) -> Union[Field, Tuple[Field, List[Field]]]:
        """
        Creates a new field with the given type for a table.

        :param user: The user on whose behalf the field is created.
        :param table: The table that the field belongs to.
        :param type_name: The type name of the field. Available types can be found in
            the field_type_registry.
        :param primary: Every table needs at least a primary field which cannot be
            deleted and is a representation of the whole row.
        :param do_schema_change: Indicates whether or not he actual database schema
            change has be made.
        :param return_updated_fields: When True any other fields who changed as a
            result of this field creation are returned with their new field instances.
        :param kwargs: The field values that need to be set upon creation.
        :type kwargs: object
        :param primary_key: The id of the field.
        :raises PrimaryFieldAlreadyExists: When we try to create a primary field,
            but one already exists.
        :raises MaxFieldLimitExceeded: When we try to create a field,
            but exceeds the field limit.
        :return: The created field instance. If return_updated_field is set then any
            updated fields as a result of creating the field are returned in a list
            as a second tuple value.
        """

        group = table.database.group
        group.has_user(user, raise_error=True)

        # Because only one primary field per table can exist and we have to check if one
        # already exists. If so the field cannot be created and an exception is raised.
        if primary and Field.objects.filter(table=table, primary=True).exists():
            raise PrimaryFieldAlreadyExists(
                f"A primary field already exists for the " f"table {table}."
            )

        # Figure out which model to use and which field types are allowed for the given
        # field type.
        field_type = field_type_registry.get(type_name)
        model_class = field_type.model_class
        allowed_fields = ["name"] + field_type.allowed_fields
        field_values = extract_allowed(kwargs, allowed_fields)
        last_order = model_class.get_last_order(table)

        num_fields = table.field_set.count()
        if (num_fields + 1) > settings.MAX_FIELD_LIMIT:
            raise MaxFieldLimitExceeded(
                f"Fields count exceeds the limit of {settings.MAX_FIELD_LIMIT}"
            )

        _validate_field_name(field_values, table)

        field_values = field_type.prepare_values(field_values, user)
        before = field_type.before_create(
            table, primary, field_values, last_order, user
        )

        instance = model_class(
            table=table,
            order=last_order,
            primary=primary,
            pk=primary_key,
            **field_values,
        )
        update_collector = CachingFieldUpdateCollector(table)
        instance.save(field_lookup_cache=update_collector, raise_if_invalid=True)
        FieldDependencyHandler.rebuild_dependencies(instance, update_collector)

        # Add the field to the table schema.
        with safe_django_schema_editor() as schema_editor:
            to_model = instance.table.get_model(field_ids=[], fields=[instance])
            model_field = to_model._meta.get_field(instance.db_column)

            if do_schema_change:
                schema_editor.add_field(to_model, model_field)

        field_type.after_create(instance, to_model, user, connection, before)

        update_collector.cache_model_fields(to_model)
        for (
            dependant_field,
            dependant_field_type,
            via_path_to_starting_table,
        ) in instance.dependant_fields_with_types(field_cache=update_collector):
            dependant_field_type.field_dependency_created(
                dependant_field,
                instance,
                via_path_to_starting_table,
                update_collector,
            )

        updated_fields = update_collector.apply_updates_and_get_updated_fields()

        field_created.send(
            self,
            field=instance,
            user=user,
            related_fields=updated_fields,
            type_name=type_name,
        )
        update_collector.send_additional_field_updated_signals()

        return (instance, updated_fields) if return_updated_fields else instance

    def update_field(
        self,
        user: AbstractUser,
        field: SpecificFieldForUpdate,
        new_type_name: Optional[str] = None,
        return_updated_fields: bool = False,
        postfix_to_fix_name_collisions: Optional[str] = None,
        after_schema_change_callback: Optional[
            Callable[[SpecificFieldForUpdate], None]
        ] = None,
        **kwargs,
    ) -> Union[SpecificFieldForUpdate, Tuple[SpecificFieldForUpdate, List[Field]]]:
        """
        Updates the values and/or type of the given field.

        :param user: The user on whose behalf the table is updated.
        :param field: The field instance that needs to be updated.
        :param new_type_name: If the type needs to be changed it can be
            provided here.
        :param return_updated_fields: When True any other fields who changed as a
            result of this field update are returned with their new field instances.
        :param postfix_to_fix_name_collisions: If provided and the field name
            already exists in the table, the specified postfix will be added to the
            field name and possibly incremented until a unique unused name is found.
            If this parameter is not set instead an exception will be raised if
            the field name already exists.
        :param after_schema_change_callback: If specified this callback is called
            after the field has had it's schema updated but before any dependant
            fields have been updated.
        :param kwargs: The field values that need to be updated
        :raises ValueError: When the provided field is not an instance of Field.
        :raises CannotChangeFieldType: When the database server responds with an
            error while trying to change the field type. This should rarely happen
            because of the lenient schema editor, which replaces the value with null
            if it could not be converted.
        :return: A data class containing information on all the changes made as a result
            of the field update.
        """

        if not isinstance(field, Field):
            raise ValueError("The field is not an instance of Field.")

        if type(field) is Field:
            raise ValueError(
                "The field must be a specific instance of Field and not the base type "
                "Field itself."
            )

        group = field.table.database.group
        group.has_user(user, raise_error=True)

        old_field = deepcopy(field)
        from_field_type = field_type_registry.get_by_model(field)
        from_model = field.table.get_model(field_ids=[], fields=[field])
        to_field_type_name = new_type_name or from_field_type.type

        # If the provided field type does not match with the current one we need to
        # migrate the field to the new type. Because the type has changed we also need
        # to remove all view filters.
        baserow_field_type_changed = from_field_type.type != to_field_type_name
        if baserow_field_type_changed:
            to_field_type = field_type_registry.get(to_field_type_name)

            if field.primary and not to_field_type.can_be_primary_field:
                raise IncompatiblePrimaryFieldTypeError(to_field_type_name)

            new_model_class = to_field_type.model_class
            field.change_polymorphic_type_to(new_model_class)

            # If the field type changes it could be that some dependencies,
            # like filters or sortings need to be changed.
            ViewHandler().field_type_changed(field)
        else:
            to_field_type = from_field_type

        allowed_fields = ["name"] + to_field_type.allowed_fields
        field_values = extract_allowed(kwargs, allowed_fields)

        self._validate_name_and_optionally_rename_if_collision(
            field, field_values, postfix_to_fix_name_collisions
        )

        field_values = to_field_type.prepare_values(field_values, user)
        before = to_field_type.before_update(old_field, field_values, user)

        field = set_allowed_attrs(field_values, allowed_fields, field)

        update_collector = CachingFieldUpdateCollector(field.table)
        field.save(field_lookup_cache=update_collector, raise_if_invalid=True)
        FieldDependencyHandler.rebuild_dependencies(field, update_collector)
        # If no converter is found we are going to convert to field using the
        # lenient schema editor which will alter the field's type and set the data
        # value to null if it can't be converted.
        to_model = field.table.get_model(field_ids=[], fields=[field])
        from_model_field = from_model._meta.get_field(field.db_column)
        to_model_field = to_model._meta.get_field(field.db_column)

        # Before a field is updated we are going to call the before_schema_change
        # method of the old field because some cleanup of related instances might
        # need to happen.
        from_field_type.before_schema_change(
            old_field,
            field,
            from_model,
            to_model,
            from_model_field,
            to_model_field,
            user,
        )

        if converter := field_converter_registry.find_applicable_converter(
            from_model, old_field, field
        ):
            # If a field data converter is found we are going to use that one to alter
            # the field and maybe do some data conversion.
            converter.alter_field(
                old_field,
                field,
                from_model,
                to_model,
                from_model_field,
                to_model_field,
                user,
                connection,
            )
        else:
            if baserow_field_type_changed:
                # If the baserow type has changed we always want to force run any alter
                # column SQL as otherwise it might not run if the two baserow fields
                # share the same underlying database column type.
                force_alter_column = True
            else:
                force_alter_column = to_field_type.force_same_type_alter_column(
                    old_field, field
                )

            # If no field converter is found we are going to alter the field using the
            # the lenient schema editor.
            with lenient_schema_editor(
                connection,
                from_field_type.get_alter_column_prepare_old_value(
                    connection, old_field, field
                ),
                to_field_type.get_alter_column_prepare_new_value(
                    connection, old_field, field
                ),
                force_alter_column,
            ) as schema_editor:
                try:
                    schema_editor.alter_field(
                        from_model, from_model_field, to_model_field
                    )
                except (ProgrammingError, DataError) as e:
                    # If something is going wrong while changing the schema we will
                    # just raise a specific exception. In the future we want to have
                    # some sort of converter abstraction where the values of certain
                    # types can be converted to another value.
                    logger.error(str(e))
                    message = (
                        f"Could not alter field when changing field type "
                        f"{from_field_type.type} to {to_field_type_name}."
                    )
                    raise CannotChangeFieldType(message)

        from_model_field_type = from_model_field.db_parameters(connection)["type"]
        to_model_field_type = to_model_field.db_parameters(connection)["type"]
        altered_column = from_model_field_type != to_model_field_type

        # If the new field doesn't support select options we can delete those
        # relations.
        if (
            from_field_type.can_have_select_options
            and not to_field_type.can_have_select_options
        ):
            old_field.select_options.all().delete()

        to_field_type.after_update(
            old_field,
            field,
            from_model,
            to_model,
            user,
            connection,
            altered_column,
            before,
        )

        if after_schema_change_callback:
            after_schema_change_callback(field)

        update_collector.cache_model_fields(to_model)
        for (
            dependant_field,
            dependant_field_type,
            via_path_to_starting_table,
        ) in field.dependant_fields_with_types(field_cache=update_collector):
            dependant_field_type.field_dependency_updated(
                dependant_field,
                field,
                old_field,
                via_path_to_starting_table,
                update_collector,
            )

        updated_fields = update_collector.apply_updates_and_get_updated_fields()

        ViewHandler().field_updated(field)

        field_updated.send(
            self,
            field=field,
            old_field=old_field,
            related_fields=updated_fields,
            user=user,
        )
        update_collector.send_additional_field_updated_signals()

        return (field, updated_fields) if return_updated_fields else field

    def delete_field(
        self,
        user: AbstractUser,
        field: Field,
        create_separate_trash_entry=True,
        update_collector=None,
        apply_and_send_updates=True,
        allow_deleting_primary=False,
    ) -> List[Field]:
        """
        Deletes an existing field if it is not a primary field.

        :param user: The user on whose behalf the table is created.
        :param field: The field instance that needs to be deleted.
        :param create_separate_trash_entry: True if this deletion should create a trash
            entry just for this one field. This should be false only when this field is
            being deleted as part of a parent item whose trash entry will restore
            this field.
        :param update_collector: An optional update collector which will be used to
            store related field updates in.
        :param apply_and_send_updates: Set to False to disable related field updates
            being applied and any signals from being sent.
        :param allow_deleting_primary: Set to true if its OK for a primary field to be
            deleted.
        :raises ValueError: When the provided field is not an instance of Field.
        :raises CannotDeletePrimaryField: When we try to delete the primary field
            which cannot be deleted.
        """

        if not isinstance(field, Field):
            raise ValueError("The field is not an instance of Field")

        group = field.table.database.group
        group.has_user(user, raise_error=True)

        if field.primary and not allow_deleting_primary:
            raise CannotDeletePrimaryField(
                "Cannot delete the primary field of a table."
            )

        field = field.specific

        if update_collector is None:
            update_collector = CachingFieldUpdateCollector(field.table)

        dependant_fields = field.dependant_fields_with_types(
            field_cache=update_collector
        )

        before_return = before_field_deleted.send(
            self,
            field_id=field.id,
            field=field,
            user=user,
        )

        TrashHandler.trash(
            user,
            group,
            field.table.database,
            field,
            create_trash_entry=create_separate_trash_entry,
        )

        FieldDependencyHandler.break_dependencies_delete_dependants(field)

        for (
            dependant_field,
            dependant_field_type,
            via_path_to_starting_table,
        ) in dependant_fields:
            dependant_field_type.field_dependency_deleted(
                dependant_field,
                field,
                via_path_to_starting_table,
                update_collector,
            )

        if apply_and_send_updates:
            updated_fields = update_collector.apply_updates_and_get_updated_fields()
            field_deleted.send(
                self,
                field_id=field.id,
                field=field,
                related_fields=updated_fields,
                user=user,
                before_return=before_return,
            )
            update_collector.send_additional_field_updated_signals()
            return updated_fields
        else:
            return []

    def update_field_select_options(self, user, field, select_options):
        """
        Brings the select options in the desired provided state in a query efficient
        manner.

        Example: select_options = [
            {'id': 1, 'value': 'Option 1', 'color': 'blue'},
            {'value': 'Option 2', 'color': 'red'}
        ]

        :param user: The user on whose behalf the change is made.
        :type user: User
        :param field: The field of which the select options must be updated.
        :type field: Field
        :param select_options: A list containing dicts with the desired select options.
        :type select_options: list
        """

        group = field.table.database.group
        group.has_user(user, raise_error=True)

        field_type = field_type_registry.get_by_model(field)

        existing_option_ids = [existing.id for existing in field.select_options.all()]

        to_update = []
        to_create = []
        for select_option in select_options:
            create_if_not_exists_id = select_option.get(UPSERT_OPTION_DICT_KEY)
            if (
                create_if_not_exists_id is not None
                and create_if_not_exists_id in existing_option_ids
            ):
                to_update.append(create_if_not_exists_id)
            elif create_if_not_exists_id is not None or "id" not in select_option:
                to_create.append(select_option)
            else:
                to_update.append(select_option["id"])
        # Checks which option ids must be deleted by comparing the existing ids with
        # the provided ids.
        to_delete = [
            existing for existing in existing_option_ids if existing not in to_update
        ]

        # Call field_type hook before applying modifications
        field_type.before_field_options_update(
            field,
            to_create=to_create,
            to_update=to_update,
            to_delete=to_delete,
        )

        if to_delete:
            SelectOption.objects.filter(field=field, id__in=to_delete).delete()

        instance_to_create = []
        for order, select_option in enumerate(select_options):
            upsert_id = select_option.pop(UPSERT_OPTION_DICT_KEY, None)
            id = select_option.pop("id", upsert_id)
            if id in existing_option_ids:
                select_option.pop("order", None)
                # Update existing options
                field.select_options.filter(id=id).update(**select_option, order=order)
            else:
                # Create new instance
                instance_to_create.append(
                    SelectOption(
                        id=upsert_id,
                        field=field,
                        order=order,
                        value=select_option["value"],
                        color=select_option["color"],
                    )
                )

        if instance_to_create:
            SelectOption.objects.bulk_create(instance_to_create)

        # The model has changed when the select options have changed, so we need to
        # invalidate the model cache.
        field.invalidate_table_model_cache()

    # noinspection PyMethodMayBeStatic
    def find_next_unused_field_name(
        self,
        table,
        field_names_to_try: List[str],
        field_ids_to_ignore: Optional[List[int]] = None,
    ) -> str:
        """
        Finds a unused field name in the provided table. If no names in the provided
        field_names_to_try list are available then the last field name in that list will
        have a number appended which ensures it is an available unique field name.
        Respects the maximally allowed field name length. In case the field_names_to_try
        are longer than that, they will get truncated to the maximally allowed length.

        :param table: The table whose fields to search.
        :param field_names_to_try: The field_names to try in order before starting to
            append a number.
        :param field_ids_to_ignore: A list of field id's to exclude from checking to see
            if the field name clashes with.
        :return: An available field name
        """

        if field_ids_to_ignore is None:
            field_ids_to_ignore = []

        max_field_name_length = Field.get_max_name_length()

        # If the field_name_to_try is longer than the maximally allowed
        # field name length the name needs to be truncated.
        field_names_to_try = [
            item[:max_field_name_length] for item in field_names_to_try
        ]

        # Check if any of the names to try are available by finding any existing field
        # names with the same name.
        taken_field_names = set(
            Field.objects.exclude(id__in=field_ids_to_ignore)
            .filter(table=table, name__in=field_names_to_try)
            .values("name")
            .distinct()
            .values_list("name", flat=True)
        )
        # If there are more names to try than the ones used in the table then there must
        # be one which isn't used.
        if len(set(field_names_to_try)) > len(taken_field_names):
            # Loop over to ensure we maintain the ordering provided by
            # field_names_to_try, so we always return the first available name and
            # not any.
            for field_name in field_names_to_try:
                if field_name not in taken_field_names:
                    return field_name

        # None of the names in the param list are available, now using the last one lets
        # append a number to the name until we find a free one.
        original_field_name = field_names_to_try[-1]

        # Lookup any existing field names. This way we can skip these and ensure our
        # new field has a unique name.
        existing_field_name_collisions = set(
            Field.objects.exclude(id__in=field_ids_to_ignore)
            .filter(table=table)
            .order_by("name")
            .distinct()
            .values_list("name", flat=True)
        )
        i = 2
        while True:
            suffix_to_append = f" {i}"
            suffix_length = len(suffix_to_append)
            length_of_original_field_name_plus_suffix = (
                len(original_field_name) + suffix_length
            )

            # At this point we know, that the original_field_name can only
            # be maximally the length of max_field_name_length. Therefore
            # if the length_of_original_field_name_plus_suffix is longer
            # we can further truncate the field_name by the length of the
            # suffix.
            if length_of_original_field_name_plus_suffix > max_field_name_length:
                field_name = f"{original_field_name[:-suffix_length]}{suffix_to_append}"
            else:
                field_name = f"{original_field_name}{suffix_to_append}"

            i += 1
            if field_name not in existing_field_name_collisions:
                return field_name

    def restore_field(
        self,
        field: Field,
        update_collector: Optional[CachingFieldUpdateCollector] = None,
        apply_and_send_updates: bool = True,
    ):
        """
        Restores the provided field from being in the trashed state.

        :param field: The trashed field to restore.
        :param update_collector: An optional update collector that will be used to
            collect any resulting field updates due to the restore.
        :param apply_and_send_updates: Whether or not a field_restored signal should be
            sent after restoring this field.
        :raises CantRestoreTrashedItem: Raised when this field cannot yet be restored
            due to other trashed items.
        """

        field_type = field_type_registry.get_by_model(field)
        try:
            other_fields_that_must_restore_at_same_time = (
                field_type.get_other_fields_to_trash_restore_always_together(field)
            )
            for other_required_field in other_fields_that_must_restore_at_same_time:
                if other_required_field.table.trashed:
                    raise RelatedTableTrashedException()

            field.name = self.find_next_unused_field_name(
                field.table,
                [field.name, f"{field.name} (Restored)"],
                [field.id],  # Ignore the field itself from the check.
            )
            # We need to set the specific field's name also so when the field_restored
            # serializer switches to serializing the specific instance it picks up and
            # uses the new name set here rather than the name currently in the DB.
            field = field.specific
            field.name = field.name
            field.trashed = False
            if update_collector is None:
                update_collector = CachingFieldUpdateCollector(field.table)
            field.save(field_lookup_cache=update_collector)

            FieldDependencyHandler.rebuild_dependencies(field, update_collector)
            for (
                dependant_field,
                dependant_field_type,
                via_path_to_starting_table,
            ) in field.dependant_fields_with_types(update_collector):
                dependant_field_type.field_dependency_created(
                    dependant_field,
                    field,
                    via_path_to_starting_table,
                    update_collector,
                )
            if apply_and_send_updates:
                updated_fields = update_collector.apply_updates_and_get_updated_fields()
                ViewHandler().field_updated(updated_fields)

                field_restored.send(
                    self, field=field, user=None, related_fields=updated_fields
                )
                update_collector.send_additional_field_updated_signals()

            for other_required_field in other_fields_that_must_restore_at_same_time:
                if other_required_field.trashed:
                    self.restore_field(other_required_field)
        except Exception as e:
            if exception_handled := field_type.restore_failed(field, e):
                field_restored.send(self, field=field, user=None, related_fields=[])
            else:
                raise e

    def get_unique_row_values(
        self, field: Field, limit: int, split_comma_separated: bool = False
    ) -> List[str]:
        """
        Returns a list of all the unique row values for a field, sorted in order of
        frequency.

        :param field: The field whose unique values are needed.
        :param limit: The maximum number of values returned.
        :param split_comma_separated: Indicates whether the text values must be split by
            comma.
        :return: A list containing the unique values sorted by frequency.
        """

        model = field.table.get_model()
        field_object = model._field_objects[field.id]
        field_type = field_object["type"]
        field = field_object["field"]

        if not field_type.can_get_unique_values:
            raise IncompatibleFieldTypeForUniqueValues(
                f"The field type `{field_object['type']}`"
            )

        # Prepare the old value sql `p_in` to convert prepare the old value to be
        # converted to string. This is the same psql that's used when converting the
        # a field type to another type, so we're sure it's converted to the right
        # "neutral" text value.
        alter_column_prepare_old_value = field_type.get_alter_column_prepare_old_value(
            connection, field, TextField()
        )
        variables = ()

        # In some cases, the `get_alter_column_prepare_old_value` returns a tuple
        # where the first part if the psql and the second variables that must be
        # safely be injected.
        if isinstance(alter_column_prepare_old_value, tuple):
            variables = alter_column_prepare_old_value[1]
            alter_column_prepare_old_value = alter_column_prepare_old_value[0]

        # Create the temporary function try cast function. This function makes sure
        # the that if the casting fails, the query doesn't fail hard, but falls back
        # `null`.
        with connection.cursor() as cursor:
            cursor.execute(sql_drop_try_cast)
            cursor.execute(
                sql_create_try_cast
                % {
                    "alter_column_prepare_old_value": alter_column_prepare_old_value
                    or "",
                    "alter_column_prepare_new_value": "",
                    "type": "text",
                },
                variables,
            )

        # If `split_comma_separated` is `True`, then we first need to explode the raw
        # column values by comma. This means that if one of the values contains a `,
        # `, it will be treated as two values. This is for example needed when
        # converting to a multiple select field.
        if split_comma_separated:
            subselect = sql.SQL(
                """
                    select
                        trim(
                            both {trimmed} from
                            unnest(
                                regexp_split_to_array(
                                    pg_temp.try_cast({column}::text), {regex}
                                )
                            )
                        ) as col
                    from
                        {table}
                    WHERE trashed = false
                """
            ).format(
                table=sql.Identifier(model._meta.db_table),
                trimmed=sql.Literal(
                    MultipleSelectConversionConfig.trim_empty_and_quote
                ),
                column=sql.Identifier(field.db_column),
                regex=sql.Literal(MultipleSelectConversionConfig.regex_split),
            )
        # Alternatively, we just want to select the raw column value.
        else:
            subselect = sql.SQL(
                """
                SELECT pg_temp.try_cast({column}::text) as col
                FROM {table}
                WHERE trashed = false
                """
            ).format(
                table=sql.Identifier(model._meta.db_table),
                column=sql.Identifier(field.db_column),
            )

        # Finally, we executed the constructed query and return the results as a list.
        query = sql.SQL(
            """
            select col
            from ({table_select}) as tmp_table
            where col != '' and col is NOT NULL
            group by col
            order by count(col) DESC
            limit {limit}
            """
        ).format(
            table_select=subselect,
            limit=sql.Literal(limit),
        )

        with connection.cursor() as cursor:
            cursor.execute(query)
            res = cursor.fetchall()

        return [x[0] for x in res]

    def _validate_name_and_optionally_rename_if_collision(
        self,
        field: Field,
        field_values: Dict[str, Any],
        postfix_for_name_collisions: Optional[str],
    ):
        """
        Validates the name of the field raising an exception if it is invalid. If the
        postfix_for_name_collisions is provided and the new name collides
        with an existing one then the provided name will be changed so it is unique
        and the update will continue. In this case the provided postfix will first be
        appended, if that is still not unique then a number will be added also.
        """

        try:
            _validate_field_name(
                field_values, field.table, field, raise_if_name_missing=False
            )
        except FieldWithSameNameAlreadyExists as e:
            if postfix_for_name_collisions is not None:
                field_values["name"] = self.find_next_unused_field_name(
                    field.table,
                    [f"{field_values['name']} {postfix_for_name_collisions}"],
                )
            else:
                raise e
