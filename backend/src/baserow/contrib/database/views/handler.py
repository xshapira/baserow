from collections import defaultdict
from dataclasses import dataclass
from copy import deepcopy
from typing import (
    Dict,
    Any,
    List,
    Optional,
    Iterable,
    Set,
    Tuple,
    Type,
    Union,
)

import jwt

from redis.exceptions import LockNotOwnedError

from django.conf import settings
from django.contrib.auth.models import AbstractUser, AnonymousUser
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.core.cache import cache
from django.db import models as django_models
from django.db.models import F, Count
from django.db.models.query import QuerySet

from baserow.contrib.database.fields.exceptions import FieldNotInTable
from baserow.contrib.database.fields.field_filters import FilterBuilder
from baserow.contrib.database.fields.field_sortings import AnnotatedOrder
from baserow.contrib.database.fields.models import Field
from baserow.contrib.database.fields.registries import field_type_registry
from baserow.contrib.database.rows.handler import RowHandler
from baserow.contrib.database.rows.signals import row_created
from baserow.contrib.database.table.models import Table, GeneratedTableModel
from baserow.core.trash.handler import TrashHandler
from baserow.core.utils import (
    extract_allowed,
    set_allowed_attrs,
    get_model_reference_field_name,
)
from .exceptions import (
    ViewDoesNotExist,
    ViewNotInTable,
    UnrelatedFieldError,
    ViewFilterDoesNotExist,
    ViewFilterNotSupported,
    ViewFilterTypeNotAllowedForField,
    ViewSortDoesNotExist,
    ViewSortNotSupported,
    ViewSortFieldAlreadyExist,
    ViewSortFieldNotSupported,
    ViewDoesNotSupportFieldOptions,
    FieldAggregationNotSupported,
    CannotShareViewTypeError,
    ViewDecorationNotSupported,
    ViewDecorationDoesNotExist,
    DecoratorValueProviderTypeNotCompatible,
    NoAuthorizationToPubliclySharedView,
)
from .models import View, ViewDecoration, ViewFilter, ViewSort
from .registries import (
    view_type_registry,
    view_filter_type_registry,
    view_aggregation_type_registry,
    decorator_type_registry,
    decorator_value_provider_type_registry,
)
from .signals import (
    view_created,
    view_updated,
    view_deleted,
    views_reordered,
    view_filter_created,
    view_filter_updated,
    view_filter_deleted,
    view_sort_created,
    view_sort_updated,
    view_sort_deleted,
    view_decoration_created,
    view_decoration_updated,
    view_decoration_deleted,
    view_field_options_updated,
)
from .validators import EMPTY_VALUES


FieldOptionsDict = Dict[int, Dict[str, Any]]


class ViewHandler:
    PUBLIC_VIEW_TOKEN_ALGORITHM = "HS256"  # nosec

    def get_view(
        self,
        view_id: int,
        view_model: Optional[Type[View]] = None,
        base_queryset: Optional[QuerySet] = None,
    ) -> View:
        """
        Selects a view and checks if the user has access to that view.
        If everything is fine the view is returned.

        :param view_id: The identifier of the view that must be returned.
        :param view_model: If provided that models objects are used to select the
            view. This can for example be useful when you want to select a GridView or
            other child of the View model.
        :param base_queryset: The base queryset from where to select the view
            object. This can for example be used to do a `select_related`. Note that
            if this is used the `view_model` parameter doesn't work anymore.
        :raises ViewDoesNotExist: When the view with the provided id does not exist.
        :return: the view instance.
        """

        if view_model is None:
            view_model = View

        if base_queryset is None:
            base_queryset = view_model.objects.all()

        try:
            view = base_queryset.select_related("table__database__group").get(
                pk=view_id
            )
        except View.DoesNotExist as exc:
            raise ViewDoesNotExist(
                f"The view with id {view_id} does not exist."
            ) from exc

        if TrashHandler.item_has_a_trashed_parent(view.table, check_item_also=True):
            raise ViewDoesNotExist(f"The view with id {view_id} does not exist.")

        return view

    def get_view_for_update(
        self,
        view_id: int,
        view_model: Optional[Type[View]] = None,
        base_queryset: Optional[QuerySet] = None,
    ) -> View:
        """
        Selects a view for update and checks if the user has access to that view.
        If everything is fine the view is returned.

        :param view_id: The identifier of the view that must be returned.
        :param view_model: If provided that models objects are used to select the
            view. This can for example be useful when you want to select a GridView or
            other child of the View model.
        :param base_queryset: The base queryset from where to select the view
            object. This can for example be used to do a `select_related`. Note that
            if this is used the `view_model` parameter doesn't work anymore.
        :raises ViewDoesNotExist: When the view with the provided id does not exist.
        :return: the view instance.
        """

        if view_model is None:
            view_model = View

        if base_queryset is None:
            tables_to_lock = ("self", ) if view_model is View else ("self", "view_ptr_id")
            base_queryset = view_model.objects.select_for_update(of=tables_to_lock)

        return self.get_view(view_id, view_model, base_queryset)

    def create_view(
        self, user: AbstractUser, table: Table, type_name: str, **kwargs
    ) -> View:
        """
        Creates a new view based on the provided type.

        :param user: The user on whose behalf the view is created.
        :param table: The table that the view instance belongs to.
        :param type_name: The type name of the view.
        :param kwargs: The fields that need to be set upon creation.
        :return: The created view instance.
        """

        group = table.database.group
        group.has_user(user, raise_error=True)

        # Figure out which model to use for the given view type.
        view_type = view_type_registry.get(type_name)
        model_class = view_type.model_class
        view_values = view_type.prepare_values(kwargs, table, user)
        allowed_fields = [
            "name",
            "filter_type",
            "filters_disabled",
        ] + view_type.allowed_fields
        view_values = extract_allowed(view_values, allowed_fields)
        last_order = model_class.get_last_order(table)

        instance = model_class.objects.create(
            table=table, order=last_order, **view_values
        )

        view_type.view_created(view=instance)
        view_created.send(self, view=instance, user=user, type_name=type_name)

        return instance

    def update_view(
        self, user: AbstractUser, view: View, **data: Dict[str, Any]
    ) -> View:
        """
        Updates an existing view instance.

        :param user: The user on whose behalf the view is updated.
        :param view: The view instance that needs to be updated.
        :param data: The fields that need to be updated.
        :raises ValueError: When the provided view not an instance of View.
        :return: The updated view instance.
        """

        if not isinstance(view, View):
            raise ValueError("The view is not an instance of View.")

        group = view.table.database.group
        group.has_user(user, raise_error=True)

        view_type = view_type_registry.get_by_model(view)
        view_values = view_type.prepare_values(data, view.table, user)
        allowed_fields = [
            "name",
            "filter_type",
            "filters_disabled",
            "public_view_password",
        ] + view_type.allowed_fields
        view = set_allowed_attrs(view_values, allowed_fields, view)
        view.save()

        if "filters_disabled" in view_values:
            view_type.after_filter_update(view)

        view_updated.send(self, view=view, user=user)

        return view

    def order_views(self, user: AbstractUser, table: Table, order: List[int]):
        """
        Updates the order of the views in the given table. The order of the views
        that are not in the `order` parameter set set to `0`.

        :param user: The user on whose behalf the views are ordered.
        :param table: The table of which the views must be updated.
        :param order: A list containing the view ids in the desired order.
        :raises ViewNotInTable: If one of the view ids in the order does not belong
            to the table.
        """

        group = table.database.group
        group.has_user(user, raise_error=True)

        queryset = View.objects.filter(table_id=table.id)
        view_ids = queryset.values_list("id", flat=True)

        for view_id in order:
            if view_id not in view_ids:
                raise ViewNotInTable(view_id)

        View.order_objects(queryset, order)
        views_reordered.send(self, table=table, order=order, user=user)

    def get_views_order(self, user: AbstractUser, table: Table):
        """
        Returns the order of the views in the given table.

        :param user: The user on whose behalf the views are ordered.
        :param table: The table of which the views must be updated.
        :raises ViewNotInTable: If one of the view ids in the order does not belong
            to the table.
        """

        group = table.database.group
        group.has_user(user, raise_error=True)

        queryset = View.objects.filter(table_id=table.id)

        order = queryset.values_list("id", flat=True)
        order = list(order)

        return order

    def delete_view_by_id(self, user: AbstractUser, view_id: int):
        """
        Trashes an existing view instance.

        :param user: The user on whose behalf the view is deleted.
        :param view_id: The view instance id that needs to be deleted.
        """

        view = self.get_view_for_update(view_id)
        self.delete_view(user, view)

    def delete_view(self, user: AbstractUser, view: View):
        """
        Trashes an existing view instance.

        :param user: The user on whose behalf the view is deleted.
        :param view: The view instance that needs to be deleted.
        :raises ViewDoesNotExist: When the view with the provided id does not exist.
        """

        if not isinstance(view, View):
            raise ValueError("The view is not an instance of View")

        group = view.table.database.group
        group.has_user(user, raise_error=True)

        view_id = view.id

        TrashHandler().trash(user, group, view.table.database, view)

        view_deleted.send(self, view_id=view_id, view=view, user=user)

    def update_field_options(
        self,
        view: View,
        field_options: FieldOptionsDict,
        user: Optional[AbstractUser] = None,
        fields: Optional[QuerySet[Field]] = None,
    ):
        """
        Updates the field options with the provided values if the field id exists in
        the table related to the view.

        This will also update views which are trashed. It is up to the caller to
        ensure that the view is not trashed if they would like to exclude it from
        the update.

        It is necesarry to do so, because aggregations have to be removed
        from trashed views as well if the field options change. Otherwise,
        you might restore a view and the aggregation is invalid on that view.

        :param view: The view for which the field options need to be updated.
        :param field_options: A dict with the field ids as the key and a dict
            containing the values that need to be updated as value.
        :param user: Optionally the user on whose behalf the request is made. If you
          give a user, the permissions are checked against this user otherwise there is
          no permission checking.
        :param fields: Optionally a list of fields can be provided so that they don't
            have to be fetched again.
        :raises UnrelatedFieldError: When the provided field id is not related to the
            provided view.
        """

        if user is not None:
            # Here we check the permissions only if we have a user. If the field options
            # update is triggered by user a action, we have one from the view but in
            # some situation, we have automatic processing and we don't have any user.
            view.table.database.group.has_user(user, raise_error=True)

        if not fields:
            fields = Field.objects.filter(table=view.table)

        try:
            model = view._meta.get_field("field_options").remote_field.through
        except FieldDoesNotExist as exc:
            raise ViewDoesNotSupportFieldOptions(
                "This view does not support field options."
            ) from exc

        field_name = get_model_reference_field_name(model, View)

        if not field_name:
            raise ValueError(
                "The model doesn't have a relationship with the View model or any "
                "descendants."
            )

        view_type = view_type_registry.get_by_model(view.specific_class)
        field_options = view_type.before_field_options_update(
            view, field_options, fields
        )

        allowed_field_ids = [field.id for field in fields]
        for field_id, options in field_options.items():
            if int(field_id) not in allowed_field_ids:
                raise UnrelatedFieldError(
                    f"The field id {field_id} is not related to the view."
                )
            model.objects_and_trash.update_or_create(
                field_id=field_id, defaults=options, **{field_name: view}
            )

        view_field_options_updated.send(self, view=view, user=user)

    def field_type_changed(self, field: Field):
        """
        This method is called by the FieldHandler when the field type of a field has
        changed. It could be that the field has filters or sortings that are not
        compatible anymore. If that is the case then those need to be removed.
        All view_type `after_field_type_change` of views that are linked to this field
        are also called to react on this change.

        :param field: The new field object.
        """

        field_type = field_type_registry.get_by_model(field.specific_class)

        # If the new field type does not support sorting then all sortings will be
        # removed.
        if not field_type.check_can_order_by(field):
            field.viewsort_set.all().delete()

        # Check which filters are not compatible anymore and remove those.
        for filter in field.viewfilter_set.all():
            filter_type = view_filter_type_registry.get(filter.type)

            if not filter_type.field_is_compatible(field):
                filter.delete()

        # Call view types hook
        for view_type in view_type_registry.get_all():
            view_type.after_field_type_change(field)

        for (
            decorator_value_provider_type
        ) in decorator_value_provider_type_registry.get_all():
            decorator_value_provider_type.after_field_type_change(field)

    def field_value_updated(self, updated_fields: Union[Iterable[Field], Field]):
        """
        Called after a field value has been modified because of a row creation,
        modification, deletion. This method is called for each directly or indirectly
        affected list of fields.

        Calls the `.after_field_value_update(updated_fields)` of each view type.

        :param updated_fields: The field or list of fields that are affected.
        """

        if not isinstance(updated_fields, list):
            updated_fields = [updated_fields]

        # Call each view types hook
        for view_type in view_type_registry.get_all():
            view_type.after_field_value_update(updated_fields)

    def field_updated(self, updated_fields: Union[Iterable[Field], Field]):
        """
        Called for each field modification. This include indirect modification when
        fields depends from another (like formula fields or lookup fields).

        Calls the `.after_field_update(updated_fields)` of each view type.

        :param updated_fields: The field or list of fields that are updated.
        """

        if not isinstance(updated_fields, list):
            updated_fields = [updated_fields]

        # Call each view types hook
        for view_type in view_type_registry.get_all():
            view_type.after_field_update(updated_fields)

    def _get_filter_builder(
        self, view: View, model: GeneratedTableModel
    ) -> FilterBuilder:
        """
        Constructs a FilterBuilder object based on the provided view's filter.

        :param view: The view where to fetch the fields from.
        :param model: The generated model containing all fields.
        :return: FilterBuilder object with the view's filter applied.
        """

        # The table model has to be dynamically generated
        if not hasattr(model, "_field_objects"):
            raise ValueError("A queryset of the table model is required.")

        filter_builder = FilterBuilder(filter_type=view.filter_type)
        for view_filter in view.viewfilter_set.all():
            if view_filter.field_id not in model._field_objects:
                raise ValueError(
                    f"The table model does not contain field "
                    f"{view_filter.field_id}."
                )
            field_object = model._field_objects[view_filter.field_id]
            field_name = field_object["name"]
            model_field = model._meta.get_field(field_name)
            view_filter_type = view_filter_type_registry.get(view_filter.type)
            filter_builder.filter(
                view_filter_type.get_filter(
                    field_name, view_filter.value, model_field, field_object["field"]
                )
            )

        return filter_builder

    def apply_filters(self, view: View, queryset: QuerySet) -> QuerySet:
        """
        Applies the view's filter to the given queryset.

        :param view: The view where to fetch the fields from.
        :param queryset: The queryset where the filters need to be applied to.
        :raises ValueError: When the queryset's model is not a table model or if the
            table model does not contain the one of the fields.
        :return: The queryset where the filters have been applied to.
        """

        model = queryset.model

        if view.filters_disabled:
            return queryset

        filter_builder = self._get_filter_builder(view, model)
        return filter_builder.apply_to_queryset(queryset)

    def get_filter(
        self,
        user: AbstractUser,
        view_filter_id: int,
        base_queryset: Optional[QuerySet] = None,
    ) -> ViewFilter:
        """
        Returns an existing view filter by the given id.

        :param user: The user on whose behalf the view filter is requested.
        :param view_filter_id: The id of the view filter.
        :param base_queryset: The base queryset from where to select the view filter
            object. This can for example be used to do a `select_related`.
        :raises ViewFilterDoesNotExist: The requested view does not exists.
        :return: The requested view filter instance.
        """

        if base_queryset is None:
            base_queryset = ViewFilter.objects

        try:
            view_filter = base_queryset.select_related(
                "view__table__database__group"
            ).get(pk=view_filter_id)
        except ViewFilter.DoesNotExist:
            raise ViewFilterDoesNotExist(
                f"The view filter with id {view_filter_id} does not exist."
            )

        if TrashHandler.item_has_a_trashed_parent(
            view_filter.view, check_item_also=True
        ):
            raise ViewFilterDoesNotExist(
                f"The view filter with id {view_filter_id} does not exist."
            )

        group = view_filter.view.table.database.group
        group.has_user(user, raise_error=True)

        return view_filter

    def create_filter(
        self,
        user: AbstractUser,
        view: View,
        field: Field,
        type_name: str,
        value: str,
        primary_key: Optional[int] = None,
    ) -> ViewFilter:
        """
        Creates a new view filter. The rows that are visible in a view should always
        be filtered by the related view filters.

        :param user: The user on whose behalf the view filter is created.
        :param view: The view for which the filter needs to be created.
        :param field: The field that the filter should compare the value with.
        :param type_name: The filter type, allowed values are the types in the
            view_filter_type_registry `equal`, `not_equal` etc.
        :param value: The value that the filter must apply to.
        :param primary_key: An optional primary key to give to the new view filter.
        :raises ViewFilterNotSupported: When the provided view does not support
            filtering.
        :raises ViewFilterTypeNotAllowedForField: When the field does not support the
            filter type.
        :raises FieldNotInTable:  When the provided field does not belong to the
            provided view's table.
        :return: The created view filter instance.
        """

        group = view.table.database.group
        group.has_user(user, raise_error=True)

        # Check if view supports filtering
        view_type = view_type_registry.get_by_model(view.specific_class)
        if not view_type.can_filter:
            raise ViewFilterNotSupported(
                f"Filtering is not supported for {view_type.type} views."
            )

        view_filter_type = view_filter_type_registry.get(type_name)
        field_type = field_type_registry.get_by_model(field.specific_class)

        # Check if the field is allowed for this filter type.
        if not view_filter_type.field_is_compatible(field):
            raise ViewFilterTypeNotAllowedForField(type_name, field_type.type)

        # Check if field belongs to the grid views table
        if not view.table.field_set.filter(id=field.pk).exists():
            raise FieldNotInTable(
                f"The field {field.pk} does not belong to table {view.table.id}."
            )

        view_filter = ViewFilter.objects.create(
            pk=primary_key,
            view=view,
            field=field,
            type=view_filter_type.type,
            value=value,
        )

        # Call view type hooks
        view_type.after_filter_update(view)

        view_filter_created.send(self, view_filter=view_filter, user=user)

        return view_filter

    def update_filter(
        self,
        user: AbstractUser,
        view_filter: ViewFilter,
        field: Field = None,
        type_name: str = None,
        value: str = None,
    ) -> ViewFilter:
        """
        Updates the values of an existing view filter.

        :param user: The user on whose behalf the view filter is updated.
        :param view_filter: The view filter that needs to be updated.
        :param field: The model of the field to filter by.
        :param type_name: Indicates how the field's value must be compared
        to the filter's value.
        :param value: The filter value that must be compared to the field's value.
        :raises ViewFilterTypeNotAllowedForField: When the field does not supports the
            filter type.
        :raises FieldNotInTable: When the provided field does not belong to the
            view's table.
        :return: The updated view filter instance.
        """

        group = view_filter.view.table.database.group
        group.has_user(user, raise_error=True)

        type_name = type_name if type_name is not None else view_filter.type
        field = field if field is not None else view_filter.field
        value = value if value is not None else view_filter.value
        view_filter_type = view_filter_type_registry.get(type_name)
        field_type = field_type_registry.get_by_model(field.specific_class)

        # Check if the field is allowed for this filter type.
        if not view_filter_type.field_is_compatible(field):
            raise ViewFilterTypeNotAllowedForField(type_name, field_type.type)

        # If the field has changed we need to check if the field belongs to the table.
        if (
            field.id != view_filter.field_id
            and not view_filter.view.table.field_set.filter(id=field.pk).exists()
        ):
            raise FieldNotInTable(
                f"The field {field.pk} does not belong to table "
                f"{view_filter.view.table.id}."
            )

        view_filter.field = field
        view_filter.value = value
        view_filter.type = type_name
        view_filter.save()

        # Call view type hooks
        view_type = view_type_registry.get_by_model(view_filter.view.specific_class)
        view_type.after_filter_update(view_filter.view)

        view_filter_updated.send(self, view_filter=view_filter, user=user)

        return view_filter

    def delete_filter(self, user: AbstractUser, view_filter: ViewFilter):
        """
        Deletes an existing view filter.

        :param user: The user on whose behalf the view filter is deleted.
        :param view_filter: The view filter instance that needs to be deleted.
        """

        group = view_filter.view.table.database.group
        group.has_user(user, raise_error=True)

        view_filter_id = view_filter.id
        view_filter.delete()

        # Call view type hooks
        view_type = view_type_registry.get_by_model(view_filter.view.specific_class)
        view_type.after_filter_update(view_filter.view)

        view_filter_deleted.send(
            self, view_filter_id=view_filter_id, view_filter=view_filter, user=user
        )

    def apply_sorting(
        self,
        view: View,
        queryset: QuerySet,
        restrict_to_field_ids: Optional[Iterable[int]] = None,
    ) -> QuerySet:
        """
        Applies the view's sorting to the given queryset. The first sort, which for now
        is the first created, will always be applied first. Secondary sortings are
        going to be applied if the values of the first sort rows are the same.

        Example:

        id | field_1 | field_2
        1  | Bram    | 20
        2  | Bram    | 10
        3  | Elon    | 30

        If we are going to sort ascending on field_1 and field_2 the resulting ids are
        going to be 2, 1 and 3 in that order.

        :param view: The view where to fetch the sorting from.
        :param queryset: The queryset where the sorting need to be applied to.
        :raises ValueError: When the queryset's model is not a table model or if the
            table model does not contain the one of the fields.
        :raises ViewSortDoesNotExist: When the view is trashed
        :param restrict_to_field_ids: Only field ids in this iterable will have their
            view sorts applied in the resulting queryset.
        :return: The queryset where the sorting has been applied to.
        """

        model = queryset.model

        # If the model does not have the `_field_objects` property then it is not a
        # generated table model which is not supported.
        if not hasattr(model, "_field_objects"):
            raise ValueError("A queryset of the table model is required.")

        if view.trashed:
            raise ViewSortDoesNotExist(f"The view {view.id} is trashed.")

        order_by = []

        qs = view.viewsort_set
        if restrict_to_field_ids is not None:
            qs = qs.filter(field_id__in=restrict_to_field_ids)
        for view_sort in qs.all():
            # If the to be sort field is not present in the `_field_objects` we
            # cannot filter so we raise a ValueError.
            if view_sort.field_id not in model._field_objects:
                raise ValueError(
                    f"The table model does not contain field {view_sort.field_id}."
                )

            field = model._field_objects[view_sort.field_id]["field"]
            field_name = model._field_objects[view_sort.field_id]["name"]
            field_type = model._field_objects[view_sort.field_id]["type"]

            order = field_type.get_order(field, field_name, view_sort.order)
            annotation = None

            if isinstance(order, AnnotatedOrder):
                annotation = order.annotation
                order = order.order

            if annotation is not None:
                queryset = queryset.annotate(**annotation)

            # If the field type does not have a specific ordering expression we can
            # order the default way.
            if not order:
                order = F(field_name)

                if view_sort.order == "ASC":
                    order = order.asc(nulls_first=True)
                else:
                    order = order.desc(nulls_last=True)

            order_by.append(order)

        order_by.extend(("order", "id"))
        queryset = queryset.order_by(*order_by)

        return queryset

    def get_sort(self, user, view_sort_id, base_queryset=None):
        """
        Returns an existing view sort with the given id.

        :param user: The user on whose behalf the view sort is requested.
        :type user: User
        :param view_sort_id: The id of the view sort.
        :type view_sort_id: int
        :param base_queryset: The base queryset from where to select the view sort
            object from. This can for example be used to do a `select_related`.
        :type base_queryset: Queryset
        :raises ViewSortDoesNotExist: The requested view does not exists.
        :return: The requested view sort instance.
        :type: ViewSort
        """

        if base_queryset is None:
            base_queryset = ViewSort.objects

        try:
            view_sort = base_queryset.select_related(
                "view__table__database__group"
            ).get(pk=view_sort_id)
        except ViewSort.DoesNotExist:
            raise ViewSortDoesNotExist(
                f"The view sort with id {view_sort_id} does not exist."
            )

        if TrashHandler.item_has_a_trashed_parent(view_sort.view, check_item_also=True):
            raise ViewSortDoesNotExist(
                f"The view sort with id {view_sort_id} does not exist."
            )

        group = view_sort.view.table.database.group
        group.has_user(user, raise_error=True)

        return view_sort

    def create_sort(
        self,
        user: AbstractUser,
        view: View,
        field: Field,
        order: str,
        primary_key: Optional[int] = None,
    ) -> ViewSort:
        """
        Creates a new view sort.

        :param user: The user on whose behalf the view sort is created.
        :param view: The view for which the sort needs to be created.
        :param field: The field that needs to be sorted.
        :param order: The desired order, can either be ascending (A to Z) or
            descending (Z to A).
        :param primary_key: An optional primary key to give to the new view sort.
        :raises ViewSortNotSupported: When the provided view does not support sorting.
        :raises FieldNotInTable:  When the provided field does not belong to the
            provided view's table.
        :return: The created view sort instance.
        """

        group = view.table.database.group
        group.has_user(user, raise_error=True)

        # Check if view supports sorting.
        view_type = view_type_registry.get_by_model(view.specific_class)
        if not view_type.can_sort:
            raise ViewSortNotSupported(
                f"Sorting is not supported for {view_type.type} views."
            )

        # Check if the field supports sorting.
        field_type = field_type_registry.get_by_model(field.specific_class)
        if not field_type.check_can_order_by(field):
            raise ViewSortFieldNotSupported(
                f"The field {field.pk} does not support sorting."
            )

        # Check if field belongs to the grid views table
        if not view.table.field_set.filter(id=field.pk).exists():
            raise FieldNotInTable(
                f"The field {field.pk} does not belong to table {view.table.id}."
            )

        # Check if the field already exists as sort
        if view.viewsort_set.filter(field_id=field.pk).exists():
            raise ViewSortFieldAlreadyExist(
                f"A sort with the field {field.pk} already exists."
            )

        view_sort = ViewSort.objects.create(
            pk=primary_key, view=view, field=field, order=order
        )

        view_sort_created.send(self, view_sort=view_sort, user=user)

        return view_sort

    def update_sort(
        self,
        user: AbstractUser,
        view_sort: ViewSort,
        field: Optional[Field] = None,
        order: Optional[str] = None,
    ) -> ViewSort:
        """
        Updates the values of an existing view sort.

        :param user: The user on whose behalf the view sort is updated.
        :param view_sort: The view sort that needs to be updated.
        :param field: The field that must be sorted on.
        :param order: Indicates the sort order direction.
        :raises ViewSortDoesNotExist: When the view used by the filter is trashed.
        :raises ViewSortFieldNotSupported: When the field does not support sorting.
        :raises FieldNotInTable:  When the provided field does not belong to the
            provided view's table.
        :return: The updated view sort instance.
        """

        if view_sort.view.trashed:
            raise ViewSortDoesNotExist(f"The view {view_sort.view.id} is trashed.")

        group = view_sort.view.table.database.group
        group.has_user(user, raise_error=True)

        field = field if field is not None else view_sort.field
        order = order if order is not None else view_sort.order

        # If the field has changed we need to check if the field belongs to the table.
        if (
            field.id != view_sort.field_id
            and not view_sort.view.table.field_set.filter(id=field.pk).exists()
        ):
            raise FieldNotInTable(
                f"The field {field.pk} does not belong to table "
                f"{view_sort.view.table.id}."
            )

        # If the field has changed we need to check if the new field type supports
        # sorting.
        field_type = field_type_registry.get_by_model(field.specific_class)
        if field.id != view_sort.field_id and not field_type.check_can_order_by(field):
            raise ViewSortFieldNotSupported(
                f"The field {field.pk} does not support sorting."
            )

        # If the field has changed we need to check if the new field doesn't already
        # exist as sort.
        if (
            field.id != view_sort.field_id
            and view_sort.view.viewsort_set.filter(field_id=field.pk).exists()
        ):
            raise ViewSortFieldAlreadyExist(
                f"A sort with the field {field.pk} already exists."
            )

        view_sort.field = field
        view_sort.order = order
        view_sort.save()

        view_sort_updated.send(self, view_sort=view_sort, user=user)

        return view_sort

    def delete_sort(self, user, view_sort):
        """
        Deletes an existing view sort.

        :param user: The user on whose behalf the view sort is deleted.
        :type user: User
        :param view_sort: The view sort instance that needs to be deleted.
        :type view_sort: ViewSort
        """

        group = view_sort.view.table.database.group
        group.has_user(user, raise_error=True)

        view_sort_id = view_sort.id
        view_sort.delete()

        view_sort_deleted.send(
            self, view_sort_id=view_sort_id, view_sort=view_sort, user=user
        )

    def create_decoration(
        self,
        view: View,
        decorator_type_name: str,
        value_provider_type_name: str,
        value_provider_conf: Dict[str, Any],
        order: Optional[int] = None,
        user: Union["AbstractUser", None] = None,
        primary_key: Optional[int] = None,
    ) -> ViewDecoration:
        """
        Creates a new decoration based on the provided type.

        :param view: The view for which the filter needs to be created.
        :param decorator_type_name: The type of the decorator.
        :param value_provider_type_name: The value provider that provides the value
            to the decorator.
        :param value_provider_conf: The configuration used by the value provider to
            compute the values for the decorator.
        :param order: The order of the decoration.
        :param user: Optional user who have created the decoration.
        :param primary_key: An optional primary key to give to the new view sort.
        :return: The created view decoration instance.
        """

        # Check if view supports decoration
        view_type = view_type_registry.get_by_model(view.specific_class)
        if not view_type.can_decorate:
            raise ViewDecorationNotSupported(
                f"Decoration is not supported for {view_type.type} views."
            )

        decorator_type = decorator_type_registry.get(decorator_type_name)
        decorator_type.before_create_decoration(view, user)

        if value_provider_type_name:
            value_provider_type = decorator_value_provider_type_registry.get(
                value_provider_type_name
            )
            value_provider_type.before_create_decoration(view, user)

            if not value_provider_type.decorator_is_compatible(decorator_type):
                raise DecoratorValueProviderTypeNotCompatible(
                    f"Value provider {value_provider_type_name} is not compatible with"
                    f"the decorator type {decorator_type_name}."
                )

        if order is None:
            order = ViewDecoration.get_last_order(view)

        view_decoration = ViewDecoration.objects.create(
            pk=primary_key,
            view=view,
            type=decorator_type_name,
            value_provider_type=value_provider_type_name,
            value_provider_conf=value_provider_conf,
            order=order,
        )

        view_decoration_created.send(self, view_decoration=view_decoration, user=user)

        return view_decoration

    def get_decoration(
        self,
        view_decoration_id: int,
        base_queryset: QuerySet = None,
    ) -> ViewDecoration:
        """
        Returns an existing view decoration with the given id.

        :param view_decoration_id: The id of the view decoration.
        :param base_queryset: The base queryset from where to select the view decoration
            object from. This can for example be used to do a `select_related`.
        :raises ViewDecorationDoesNotExist: The requested view decoration does not
            exists.
        :return: The requested view decoration instance.
        """

        if base_queryset is None:
            base_queryset = ViewDecoration.objects

        try:
            view_decoration = base_queryset.select_related(
                "view__table__database__group"
            ).get(pk=view_decoration_id)
        except ViewDecoration.DoesNotExist:
            raise ViewDecorationDoesNotExist(
                f"The view decoration with id {view_decoration_id} does not exist."
            )

        if TrashHandler.item_has_a_trashed_parent(
            view_decoration.view.table, check_item_also=True
        ):
            raise ViewDecorationDoesNotExist(
                f"The view decoration with id {view_decoration_id} does not exist."
            )

        return view_decoration

    def update_decoration(
        self,
        view_decoration: ViewDecoration,
        user: Union["AbstractUser", None] = None,
        decorator_type_name: Optional[str] = None,
        value_provider_type_name: Optional[str] = None,
        value_provider_conf: Optional[Dict[str, Any]] = None,
        order: Optional[int] = None,
    ) -> ViewDecoration:
        """
        Updates the values of an existing view decoration.

        :param view_decoration: The view decoration that needs to be updated.
        :param user: Optional user who have created the decoration..
        :param decorator_type_name: The type of the decorator.
        :param value_provider_type_name: The value provider that provides the value
            to the decorator.
        :param value_provider_conf: The configuration used by the value provider to
            compute the values for the decorator.
        :param order: The order of the decoration.
        :raises ViewDecorationDoesNotExist: The requested view decoration does not
            exists.
        :raises DecoratorValueProviderTypeNotCompatible: When the decorator value
            provided is not compatible with the decorator type.
        :return: The updated view decoration instance.
        """

        if decorator_type_name is None:
            decorator_type_name = view_decoration.type
        if value_provider_type_name is None:
            value_provider_type_name = view_decoration.value_provider_type
        if value_provider_conf is None:
            value_provider_conf = view_decoration.value_provider_conf
        if order is None:
            order = view_decoration.order

        decorator_type = decorator_type_registry.get(decorator_type_name)
        decorator_type.before_update_decoration(view_decoration, user)

        if value_provider_type_name:
            value_provider_type = decorator_value_provider_type_registry.get(
                value_provider_type_name
            )
            value_provider_type.before_update_decoration(view_decoration, user)

            if not value_provider_type.decorator_is_compatible(decorator_type):
                raise DecoratorValueProviderTypeNotCompatible(
                    f"Value provider {value_provider_type_name} is not compatible with"
                    f"the decorator type {decorator_type_name}."
                )

        view_decoration.type = decorator_type_name
        view_decoration.value_provider_type = value_provider_type_name
        view_decoration.value_provider_conf = value_provider_conf
        view_decoration.order = order
        view_decoration.save()

        view_decoration_updated.send(self, view_decoration=view_decoration, user=user)

        return view_decoration

    def delete_decoration(
        self,
        view_decoration: ViewDecoration,
        user: Union["AbstractUser", None] = None,
    ):
        """
        Deletes an existing view decoration.

        :param view_decoration: The view decoration instance that needs to be deleted.
        :param user: Optional user who have deleted the decoration.
        :raises ViewDecorationDoesNotExist: The requested view decoration does not
            exists.
        """

        group = view_decoration.view.table.database.group
        group.has_user(user, raise_error=True)

        view_decoration_id = view_decoration.id
        view_decoration.delete()

        view_decoration_deleted.send(
            self,
            view_decoration_id=view_decoration_id,
            view_decoration=view_decoration,
            view_filter=view_decoration,
            user=user,
        )

    def get_queryset(
        self,
        view,
        search=None,
        model=None,
        only_sort_by_field_ids=None,
        only_search_by_field_ids=None,
    ):
        """
        Returns a queryset for the provided view which is appropriately sorted,
        filtered and searched according to the view type and its settings.

        :param search: A search term to apply to the resulting queryset.
        :param model: The model for this views table to generate the queryset from, if
            not specified then the model will be generated automatically.
        :param view: The view to get the export queryset and fields for.
        :type view: View
        :param only_sort_by_field_ids: To only sort the queryset by some fields
            provide those field ids in this optional iterable. Other fields not
            present in the iterable will not have their view sorts applied even if they
            have one.
        :type only_sort_by_field_ids: Optional[Iterable[int]]
        :param only_search_by_field_ids: To only apply the search term to some
            fields provide those field ids in this optional iterable. Other fields
             not present in the iterable will not be searched and filtered down by the
             search term.
        :type only_search_by_field_ids: Optional[Iterable[int]]
        :return: The appropriate queryset for the provided view.
        :rtype: QuerySet
        """

        if model is None:
            model = view.table.get_model()

        queryset = model.objects.all().enhance_by_fields()

        view_type = view_type_registry.get_by_model(view.specific_class)
        if view_type.can_filter:
            queryset = self.apply_filters(view, queryset)
        if view_type.can_sort:
            queryset = self.apply_sorting(view, queryset, only_sort_by_field_ids)
        if search is not None:
            queryset = queryset.search_all_fields(search, only_search_by_field_ids)
        return queryset

    def _get_aggregation_lock_cache_key(self, view: View):
        """
        Returns the aggregation lock cache key for the specified view.
        """

        return f"_aggregation__{view.pk}_lock"

    def _get_aggregation_value_cache_key(self, view: View, name: str):
        """
        Returns the aggregation value cache key for the specified view and name.
        """

        return f"aggregation_value__{view.pk}_{name}"

    def _get_aggregation_version_cache_key(self, view: View, name: str):
        """
        Returns the aggregation version cache key for the specified view and name.
        """

        return f"aggregation_version__{view.pk}_{name}"

    def clear_full_aggregation_cache(self, view: View):
        """
        Clears the cache key for the specified view.
        """

        view_type = view_type_registry.get_by_model(view.specific_class)
        aggregations = view_type.get_aggregations(view)
        cached_names = [agg[0].db_column for agg in aggregations]
        self.clear_aggregation_cache(view, cached_names)

    def clear_aggregation_cache(self, view: View, names: Union[List[str], str]):
        """
        Increments the version in cache for the specified view/name.
        """

        if not isinstance(names, list):
            names = [names]

        for name in names:
            cache_key = self._get_aggregation_version_cache_key(view, name)
            try:
                cache.incr(cache_key, 1)
            except ValueError:
                # No cache key, we create one
                cache.set(cache_key, 2)

    def _get_aggregations_to_compute(
        self,
        view: View,
        aggregations: Iterable[Tuple[django_models.Field, str]],
        no_cache: bool = False,
    ) -> Tuple[Dict[str, Any], Dict[str, Tuple[django_models.Field, str, int]]]:
        """
        Figure out which aggregation needs to be computed and which one is cached.

        Returns a tuple with:
          - a dict of field_name -> cached values for values that are in the cache
          - a dict of values that need to be computed. keys are field name and values
            are a tuple with:
            - The field instance which aggregation needs to be computed
            - The aggregation_type
            - The current version
        """

        if not no_cache:
            names = [agg[0].db_column for agg in aggregations]
            # Get value and version cache all at once
            cached_keys = [
                self._get_aggregation_value_cache_key(view, name) for name in names
            ] + [self._get_aggregation_version_cache_key(view, name) for name in names]
            cached = cache.get_many(cached_keys)
        else:
            # We don't want to use cache for search query
            cached = {}

        valid_cached_values = {}
        need_computation = {}

        # Try to get field value from cache or add it to the need_computation list
        for (field_instance, aggregation_type_name) in aggregations:
            cached_value = cached.get(
                self._get_aggregation_value_cache_key(view, field_instance.db_column),
                {"version": 0},
            )
            cached_version = cached.get(
                self._get_aggregation_version_cache_key(view, field_instance.db_column),
                1,
            )

            # If the value version and the current version are the same we don't
            # need to recompute the value.
            if cached_value["version"] == cached_version:
                valid_cached_values[field_instance.db_column] = cached_value["value"]
            else:
                need_computation[field_instance.db_column] = {
                    "instance": field_instance,
                    "aggregation_type": aggregation_type_name,
                    "version": cached_version,
                }

        return (valid_cached_values, need_computation)

    def get_view_field_aggregations(
        self,
        view: View,
        model: Union[GeneratedTableModel, None] = None,
        with_total: bool = False,
        search=None,
    ) -> Dict[str, Any]:
        """
        Returns a dict of aggregation for all aggregation configured for the view in
        parameters. Unless the search parameter is set to a non empty string,
        the aggregations values are cached when computed and must be
        invalidated when necessary.
        The dict keys are field names and value are aggregation values. The total is
        included in result if the with_total is specified.

        :param view: The view to get the field aggregation for.
        :param model: The model for this view table to generate the aggregation
            query from, if not specified then the model will be generated
            automatically.
        :param with_total: Whether the total row count should be returned in the
            result.
        :param search: the search string to considerate. If the search parameter is
            defined, we don't use the cache so we recompute aggregation on the fly.
        :raises FieldAggregationNotSupported: When the view type doesn't support
            field aggregation.
        :return: A dict of aggregation value
        """

        view_type = view_type_registry.get_by_model(view.specific_class)

        # Check if view supports field aggregation
        if not view_type.can_aggregate_field:
            raise FieldAggregationNotSupported(
                f"Field aggregation is not supported for {view_type.type} views."
            )

        aggregations = view_type.get_aggregations(view)

        (
            values,
            need_computation,
        ) = self._get_aggregations_to_compute(view, aggregations, no_cache=search)

        use_lock = hasattr(cache, "lock")
        used_lock = False
        if not search and use_lock and (need_computation or with_total):
            # Lock the cache to avoid many updates when many queries arrive at same
            # times which happens when multiple users are on the same view.
            # This lock is optional. It avoid processing but doesn't break anything
            # if it fails so the timeout is low.
            cache_lock = cache.lock(
                self._get_aggregation_lock_cache_key(view), timeout=10
            )

            cache_lock.acquire()
            # We update the cache here because maybe it has changed in the meantime
            (values, need_computation) = self._get_aggregations_to_compute(
                view, aggregations, no_cache=search
            )
            used_lock = True

        # Do we need to compute some aggregations?
        if need_computation or with_total:
            db_result = self.get_field_aggregations(
                view,
                [
                    (n["instance"], n["aggregation_type"])
                    for n in need_computation.values()
                ],
                model,
                with_total=with_total,
                search=search,
            )

            if not search:
                to_cache = {
                    self._get_aggregation_value_cache_key(view, key): {
                        "value": value,
                        "version": need_computation[key]["version"],
                    }
                    for key, value in db_result.items()
                    if key != "total"
                }

                # Let's cache the newly computed values
                cache.set_many(to_cache)

            # Merged cached values and computed one
            values.update(db_result)

        if used_lock:
            try:
                cache_lock.release()
            except LockNotOwnedError:
                # If the lock release fails, it might be because of the timeout
                # and it's been stolen so we don't really care
                pass

        return values

    def get_field_aggregations(
        self,
        view: View,
        aggregations: Iterable[Tuple[django_models.Field, str]],
        model: Union[GeneratedTableModel, None] = None,
        with_total: bool = False,
        search: Union[str, None] = None,
    ) -> Dict[str, Any]:
        """
        Returns a dict of aggregation for given (field, aggregation_type) couple list.
        The dict keys are field names and value are aggregation values. The total is
        included in result if the with_total is specified.

        :param view: The view to get the field aggregation for.
        :param aggregations: A list of (field_instance, aggregation_type).
        :param model: The model for this view table to generate the aggregation
            query from, if not specified then the model will be generated
            automatically.
        :param with_total: Whether the total row count should be returned in the
            result.
        :param search: the search string to considerate.
        :raises FieldAggregationNotSupported: When the view type doesn't support
            field aggregation.
        :raises FieldNotInTable: When one of the field doesn't belong to the specified
            view.
        :return: A dict of aggregation values
        """

        if model is None:
            model = view.table.get_model()

        queryset = model.objects.all().enhance_by_fields()

        view_type = view_type_registry.get_by_model(view.specific_class)

        # Check if view supports field aggregation
        if not view_type.can_aggregate_field:
            raise FieldAggregationNotSupported(
                f"Field aggregation is not supported for {view_type.type} views."
            )

        # Apply filters and search to have accurate aggregations
        if view_type.can_filter:
            queryset = self.apply_filters(view, queryset)
        if search is not None:
            queryset = queryset.search_all_fields(search)

        aggregation_dict = {}

        for (field_instance, aggregation_type_name) in aggregations:
            field_name = field_instance.db_column

            # Check whether the field belongs to the table.
            if field_instance.table_id != view.table_id:
                raise FieldNotInTable(
                    f"The field {field_instance.pk} does not belong to table "
                    f"{view.table.id}."
                )

            field = model._field_objects[field_instance.id]["field"]
            model_field = model._meta.get_field(field_name)

            aggregation_type = view_aggregation_type_registry.get(aggregation_type_name)

            aggregation_dict[field_name] = aggregation_type.get_aggregation(
                field_name, model_field, field
            )

        # Add total to allow further calculation on the client if required
        if with_total:
            aggregation_dict["total"] = Count("id", distinct=True)

        return queryset.aggregate(**aggregation_dict)

    def rotate_view_slug(self, user: AbstractUser, view: View) -> View:
        """
        Rotates the slug of the provided view.

        :param user: The user on whose behalf the view is updated.
        :param view: The form view instance that needs to be updated.
        :return: The updated view instance.
        """

        new_slug = View.create_new_slug()
        return self.update_view_slug(user, view, new_slug)

    def update_view_slug(self, user: AbstractUser, view: View, slug: str) -> View:
        """
        Updates the slug of the provided view.

        :param user: The user on whose behalf the view is updated.
        :param view: The form view instance that needs to be updated.
        :param slug: The new slug to use to address this view.
        :return: The updated view instance.
        :raises CannotShareViewTypeError: Raised if called for a view which does not
            support sharing.
        """

        view_type = view_type_registry.get_by_model(view.specific_class)
        if not view_type.can_share:
            raise CannotShareViewTypeError()

        group = view.table.database.group
        group.has_user(user, raise_error=True)

        view.slug = slug
        view.save()

        view_updated.send(self, view=view, user=user)

        return view

    def get_public_view_by_slug(
        self,
        user: Union[AbstractUser, AnonymousUser],
        slug: str,
        view_model: Optional[Type[View]] = None,
        authorization_token: Optional[str] = None,
        raise_authorization_error: bool = True,
    ) -> View:
        """
        Returns the view with the provided slug if it is public, if the user has
        access to the views group or provided a valid token in case the view is
        password protected.

        :param user: The user on whose behalf the view is requested.
        :param slug: The slug of the view.
        :param view_model: If provided that models objects are used to select the
            view. This can for example be useful when you want to select a GridView or
            other child of the View model.
        :param authorization_token: The token to use to access the view if the view is
            password protected and the user does not belong to the correct group.
        :param raise_authorization_error: Whether to raise an error if the user doesn't
            have access to the password protected sahred view.
        :raises ViewDoesNotExist: Raised if the view does not exist, it has been
            trashed or the view is not public and the user doesn't belong to the group.
        :raises NoAuthorizationToPubliclySharedView: raised if the view is public but
            password protected and the user belongs to another group and doesn't provide
            a valid permission_token.
        :return: The requested view with matching slug.
        """

        if view_model is None:
            view_model = View

        try:
            view = view_model.objects.select_related("table__database__group").get(
                slug=slug
            )
        except (view_model.DoesNotExist, ValidationError) as exc:
            raise ViewDoesNotExist("The view does not exist.") from exc

        if TrashHandler.item_has_a_trashed_parent(view.table, check_item_also=True):
            raise ViewDoesNotExist("The view does not exist.")

        user_in_group = user and view.table.database.group.has_user(user)

        if not user_in_group:
            if not view.public:
                raise ViewDoesNotExist("The view does not exist.")

            token_is_valid_for_this_view = (
                authorization_token
                and self.is_public_view_token_valid(view, authorization_token)
            )
            if (
                view.public_view_has_password
                and not token_is_valid_for_this_view
                and raise_authorization_error
            ):
                raise NoAuthorizationToPubliclySharedView(
                    "The view is password protected."
                )

        return view

    def submit_form_view(self, form, values, model=None, enabled_field_options=None):
        """
        Handles when a form is submitted. It will validate the data by checking if
        the required fields are provided and not empty and it will create a new row
        based on those values.

        :param form: The form view that is submitted.
        :type form: FormView
        :param values: The submitted values that need to be used when creating the row.
        :type values: dict
        :param model: If the model is already generated, it can be provided here.
        :type model: Model | None
        :param enabled_field_options: If the enabled field options have already been
            fetched, they can be provided here.
        :type enabled_field_options: QuerySet | list | None
        :return: The newly created row.
        :rtype: Model
        """

        table = form.table

        if model is None:
            model = table.get_model()

        if not enabled_field_options:
            enabled_field_options = form.active_field_options

        allowed_field_names = []
        field_errors = {}

        # Loop over all field options, find the name in the model and check if the
        # required values are provided. If not, a validation error is raised.
        for field in enabled_field_options:
            field_name = model._field_objects[field.field_id]["name"]
            allowed_field_names.append(field_name)

            if field.required and (
                field_name not in values or values[field_name] in EMPTY_VALUES
            ):
                field_errors[field_name] = ["This field is required."]

        if field_errors:
            raise ValidationError(field_errors)

        allowed_values = extract_allowed(values, allowed_field_names)
        instance = RowHandler().force_create_row(table, allowed_values, model)

        row_created.send(
            self, row=instance, before=None, user=None, table=table, model=model
        )

        return instance

    def get_public_views_row_checker(
        self,
        table,
        model,
        only_include_views_which_want_realtime_events,
        updated_field_ids=None,
    ):
        """
        Returns a CachingPublicViewRowChecker object which will have precalculated
        information about the public views in the provided table to aid with quickly
        checking which views a row in that table is visible in. If you will be updating
        the row and reusing the checker you must provide an iterable of the field ids
        that you will be updating in the row, otherwise the checker will cache the
        first check per view/row.

        :param table: The table the row is in.
        :param model: The model of the table including all fields.
        :param only_include_views_which_want_realtime_events: If True will only look
            for public views where
            ViewType.when_shared_publicly_requires_realtime_events is True.
        :param updated_field_ids: An optional iterable of field ids which will be
            updated on rows passed to the checker. If the checker is used on the same
            row multiple times and that row has been updated it will return invalid
            results unless you have correctly populated this argument.
        :return: A list of non-specific public view instances.
        """

        return CachingPublicViewRowChecker(
            table,
            model,
            only_include_views_which_want_realtime_events,
            updated_field_ids,
        )

    def restrict_row_for_view(
        self, view: View, serialized_row: Dict[str, Any]
    ) -> Dict[Any, Any]:
        """
        Removes any fields which are hidden in the view from the provided serialized
        row ensuring no data is leaked according to the views field options.

        :param view: The view to restrict the row by.
        :param serialized_row: A python dictionary which is the result of serializing
            the row containing `field_XXX` keys per field value. It must not be a
            serialized using user_field_names=True.
        :return: A copy of the serialized_row with all hidden fields removed.
        """

        return self.restrict_rows_for_view(view, [serialized_row])[0]

    def restrict_rows_for_view(
        self,
        view: View,
        serialized_rows: List[Dict[str, Any]],
        allowed_row_ids: Optional[List[int]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Removes any fields which are hidden in the view and any rows that don't match
        the allowed list of ids from the provided serializes rows ensuring no data is
        leaked.

        :param view: The view to restrict the row by.
        :param serialized_rows: A list of python dictionaries which are the result of
            serializing the rows containing `field_XXX` keys per field value. They
            must not be serialized using user_field_names=True.
        :param allowed_row_ids: A list of ids of rows that can be returned. If set to
            None, all passed rows can be returned.
        :return: A copy of the allowed serialized_rows with all hidden fields removed.
        """

        view_type = view_type_registry.get_by_model(view.specific_class)
        hidden_field_options = view_type.get_hidden_field_options(view)
        restricted_rows = []
        for serialized_row in serialized_rows:
            if allowed_row_ids is None or serialized_row["id"] in allowed_row_ids:
                row_copy = deepcopy(serialized_row)
                for hidden_field_option in hidden_field_options:
                    row_copy.pop(f"field_{hidden_field_option.field_id}", None)
                restricted_rows.append(row_copy)
        return restricted_rows

    def _get_public_view_jwt_secret(self, view: View) -> str:
        """
        This method provides the secret to encode and decode the (non-expiring) JWT
        token used in password protected public views.
        By changing the `slug` or the `public_view_password`, previous tokens cannot
        be decoded anymore so the user will be forced to the password input page.
        Server's SECRET_KEY is used to be sure that the JWT cannot be guessed.
        :param view: The public view to restric access to.
        :return: A string to use as secret to encode/decode JWT for the view.
        """

        return f"{view.slug}-{view.public_view_password}-{settings.SECRET_KEY}"

    def encode_public_view_token(self, view: View) -> str:
        """
        Create a non-expiring JWT token that authorize public requests for this view.
        :param view: The public view to restric access to.
        :return: A string to use as JWT token to authorize the access for the view.
        """

        secret = self._get_public_view_jwt_secret(view)
        return jwt.encode(
            {"slug_id": view.slug},
            key=secret,
            algorithm=self.PUBLIC_VIEW_TOKEN_ALGORITHM,
        )

    def decode_public_view_token(self, view: View, token: str) -> Dict[str, Any]:
        """
        Decode the token using the view's secret.
        :param view: The public view to restric access to.
        :param token: The JWT token to decode.
        :return: The payload decoded or, if invalid, a jwt.InvalidTokenError is raised.
        """

        secret = self._get_public_view_jwt_secret(view)
        return jwt.decode(
            token, key=secret, algorithms=[self.PUBLIC_VIEW_TOKEN_ALGORITHM]
        )

    def is_public_view_token_valid(self, view: View, token: str) -> bool:
        """
        Verify if the token provided is valid for the public view or not.
        :param view: The public view to restric access to.
        :param token: The JWT token to decode.
        :return: True if the token is valid for the view, False otherwise.
        """

        try:
            self.decode_public_view_token(view, token)
            return True
        except jwt.InvalidTokenError:
            return False


@dataclass
class PublicViewRows:
    """
    Keeps track of which rows are allowed to be sent as a public signal
    for a particular view.

    When no row ids are set it is assumed that any row id is allowed.
    """

    ALL_ROWS_ALLOWED = None

    view: View
    allowed_row_ids: Optional[Set[int]]

    def all_allowed(self):
        return self.allowed_row_ids is PublicViewRows.ALL_ROWS_ALLOWED

    def __iter__(self):
        return iter((self.view, self.allowed_row_ids))


class CachingPublicViewRowChecker:
    """
    A helper class to check which public views a row is visible in. Will pre-calculate
    upfront for a specific table which public views are always visible, which public
    views can have row check results cached for and finally will pre-construct and
    reuse querysets for performance reasons.
    """

    def __init__(
        self,
        table: Table,
        model: GeneratedTableModel,
        only_include_views_which_want_realtime_events: bool,
        updated_field_ids: Optional[Iterable[int]] = None,
    ):
        self._public_views = (
            table.view_set.filter(public=True).prefetch_related("viewfilter_set").all()
        )
        self._updated_field_ids = updated_field_ids
        self._views_with_filters = []
        self._always_visible_views = []
        self._view_row_check_cache = defaultdict(dict)
        handler = ViewHandler()
        for view in self._public_views:
            if only_include_views_which_want_realtime_events:
                view_type = view_type_registry.get_by_model(view.specific_class)
                if not view_type.when_shared_publicly_requires_realtime_events:
                    continue

            if len(view.viewfilter_set.all()) == 0:
                # If there are no view filters for this view then any row must always
                # be visible in this view
                self._always_visible_views.append(view)
            else:
                filter_qs = handler.apply_filters(view, model.objects)
                self._views_with_filters.append(
                    (
                        view,
                        filter_qs,
                        self._view_row_checks_can_be_cached(view),
                    )
                )

    def get_public_views_where_row_is_visible(self, row):
        """
        WARNING: If you are reusing the same checker and calling this method with the
        same row multiple times you must have correctly set which fields in the row
        might be updated in the checkers initials `updated_field_ids` attribute. This
        is because for a given view, if we know none of the fields it filters on
        will be updated we can cache the first check of if that row exists as any
        further changes to the row wont be affecting filtered fields. Hence
        `updated_field_ids` needs to be set if you are ever changing the row and
        reusing the same CachingPublicViewRowChecker instance.

        :param row: A row in the checkers table.
        :return: A list of views where the row is visible for this checkers table.
        """

        views = []
        for view, filter_qs, can_use_cache in self._views_with_filters:
            if can_use_cache:
                if row.id not in self._view_row_check_cache[view.id]:
                    self._view_row_check_cache[view.id][
                        row.id
                    ] = self._check_row_visible(filter_qs, row)
                if self._view_row_check_cache[view.id][row.id]:
                    views.append(view)
            elif self._check_row_visible(filter_qs, row):
                views.append(view)

        return views + self._always_visible_views

    def get_public_views_where_rows_are_visible(self, rows) -> List[PublicViewRows]:
        """
        WARNING: If you are reusing the same checker and calling this method with the
        same rows multiple times you must have correctly set which fields in the rows
        might be updated in the checkers initials `updated_field_ids` attribute. This
        is because for a given view, if we know none of the fields it filters on
        will be updated we can cache the first check of if that rows exist as any
        further changes to the rows wont be affecting filtered fields. Hence
        `updated_field_ids` needs to be set if you are ever changing the rows and
        reusing the same CachingPublicViewRowChecker instance.

        :param rows: Rows in the checkers table.
        :return: A list of PublicViewRows with view and a list of row ids where the rows
            are visible for this checkers table.
        """

        visible_views_rows = []
        row_ids = {row.id for row in rows}
        for view, filter_qs, can_use_cache in self._views_with_filters:
            if can_use_cache:
                for id in row_ids:
                    if id not in self._view_row_check_cache[view.id]:
                        visible_ids = set(self._check_rows_visible(filter_qs, rows))
                        for visible_id in visible_ids:
                            self._view_row_check_cache[view.id][visible_id] = True
                        break
                else:
                    visible_ids = row_ids

            else:
                visible_ids = set(self._check_rows_visible(filter_qs, rows))
            if len(visible_ids) > 0:
                visible_views_rows.append(PublicViewRows(view, visible_ids))

        visible_views_rows.extend(
            PublicViewRows(visible_view, PublicViewRows.ALL_ROWS_ALLOWED)
            for visible_view in self._always_visible_views
        )

        return visible_views_rows

    # noinspection PyMethodMayBeStatic
    def _check_row_visible(self, filter_qs, row):
        return filter_qs.filter(id=row.id).exists()

    # noinspection PyMethodMayBeStatic
    def _check_rows_visible(self, filter_qs, rows):
        return filter_qs.filter(id__in=[row.id for row in rows]).values_list(
            "id", flat=True
        )

    def _view_row_checks_can_be_cached(self, view):
        if self._updated_field_ids is None:
            return True
        return all(
            view_filter.field_id not in self._updated_field_ids
            for view_filter in view.viewfilter_set.all()
        )
