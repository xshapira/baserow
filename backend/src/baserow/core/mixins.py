from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import Case, When, Value, Manager
from django.db.models.fields import NOT_PROVIDED
from django.db.models.fields.mixins import FieldCacheMixin
from django.utils.functional import cached_property

from baserow.core.managers import (
    make_trash_manager,
    NoTrashManager,
    TrashOnlyManager,
)


class OrderableMixin:
    """
    This mixin introduces a set of helpers of the model is orderable by a field.
    """

    @classmethod
    def get_highest_order_of_queryset(cls, queryset, field="order"):
        """
        Returns the highest existing value of the provided field.

        :param queryset: The queryset containing the field to check.
        :type queryset: QuerySet
        :param field: The field name containing the value.
        :type field: str
        :return: The highest value in the queryset.
        :rtype: int
        """

        return queryset.aggregate(models.Max(field)).get(f"{field}__max", 0) or 0

    @classmethod
    def order_objects(cls, queryset, order, field="order"):
        """
        Changes the order of the objects in the given queryset to the desired order
        provided in the order parameter.

        :param queryset: The queryset of the objects that need to be updated.
        :type queryset: QuerySet
        :param order: A list containing the object ids in the desired order.
        :type order: list
        :param field: The name of the order column/field.
        :type field: str
        :return: The amount of objects updated.
        :rtype: int
        """

        return queryset.update(
            **{
                field: Case(
                    *[
                        When(id=id, then=Value(index + 1))
                        for index, id in enumerate(order)
                    ],
                    default=Value(0),
                )
            }
        )


class PolymorphicContentTypeMixin:
    """
    This mixin introduces a set of helpers for a model that has a polymorphic
    relationship with django's content type. Note that a foreignkey to the ContentType
    model must exist.

    Example:
        content_type = models.ForeignKey(
            ContentType,
            verbose_name='content type',
            related_name='applications',
            on_delete=models.SET(get_default_application_content_type)
        )
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if not hasattr(self.__class__, "content_type"):
            raise AttributeError(
                f"The attribute content_type doesn't exist on "
                f"{self.__class__.__name__}, but is required for the "
                f"PolymorphicContentTypeMixin."
            )

        if len(self.parent_ptrs()) > 1:
            raise AttributeError(
                f"The model "
                f"{self.__class__.__name__}, has multiple model parents, however "
                f"PolymorphicContentTypeMixin only allows a single parent and does "
                f"not support multiple inheritance."
            )

    def save(self, *args, **kwargs):
        self._ensure_content_type_is_set()
        super().save(*args, **kwargs)

    def _ensure_content_type_is_set(self):
        if not self.id and not self.content_type_id:
            self.content_type = ContentType.objects.get_for_model(self)

    @cached_property
    def specific(self):
        """Returns this instance in its most specific subclassed form."""

        self._ensure_content_type_is_set()
        content_type = ContentType.objects.get_for_id(self.content_type_id)
        model_class = self.specific_class
        if model_class is None or isinstance(self, model_class):
            return self
        else:
            return content_type.get_object_for_this_type(id=self.id)

    @cached_property
    def specific_class(self):
        """
        Return the class that this application would be if instantiated in its
        most specific form.
        """

        self._ensure_content_type_is_set()
        content_type = ContentType.objects.get_for_id(self.content_type_id)
        return content_type.model_class()

    def parent_ptrs(self):
        model = self.__class__
        concrete_model = model._meta.concrete_model
        return [p for p in concrete_model._meta.parents.values() if p]

    def all_parents_and_self(self):
        parent_ptrs = self.parent_ptrs()
        if len(parent_ptrs) == 0:
            return [self]
        elif len(parent_ptrs) == 1:
            parent_name = parent_ptrs[0].name
            return [*getattr(self, parent_name).all_parents_and_self(), self]
        else:
            raise AttributeError(
                f"The model "
                f"{self.__class__.__name__}, has multiple model parents, however "
                f"PolymorphicContentTypeMixin only allows a single parent and does "
                f"not support multiple inheritance."
            )

    def change_polymorphic_type_to(self, new_model_class):
        """
        If you for example have two polymorphic types TypeA and TypeB which both have
        unique fields, an instance with TypeA can be changed to TypeB while keeping the
        parent values and id. This method actually changes the class and sets the
        default values in the __dict__.

        :param new_model_class: The new model class that the instance must be converted
            to.
        :type new_model_class: Model
        """

        old_fields = set(list(self._meta.get_fields()))
        new_fields = set(list(new_model_class._meta.get_fields()))
        field_names_to_remove = old_fields - new_fields
        field_names_to_add = new_fields - old_fields

        all_parents_and_self = self.all_parents_and_self()
        if len(all_parents_and_self) > 1:
            # Delete the model instance one down from the root parent to preserve the
            # polymorphic base type whilst ensuring we delete all other sub polymorphic
            # types and none are left hanging in model hierarchies with multiple levels.
            all_parents_and_self[1].delete(keep_parents=True)
        self.__class__ = new_model_class
        self.content_type = ContentType.objects.get_for_model(new_model_class)

        def get_field_name(field):
            if isinstance(field, models.ForeignKey):
                return f"{field.name}_id"
            return field.name

        for field in field_names_to_remove:
            name = get_field_name(field)
            if name in self.__dict__:
                del self.__dict__[name]

            if isinstance(field, FieldCacheMixin) and field.is_cached(self):
                field.delete_cached_value(self)

        for field in field_names_to_add:
            name = get_field_name(field)
            field = new_model_class._meta.get_field(name)

            if isinstance(field, FieldCacheMixin) and field.is_cached(self):
                field.delete_cached_value(self)

            if hasattr(field, "default"):
                self.__dict__[name] = (
                    field.default if field.default != NOT_PROVIDED else None
                )

        # Because the field type has changed we need to invalidate the cached
        # properties so that they wont return the values of the old type.
        del self.specific
        del self.specific_class


class CreatedAndUpdatedOnMixin(models.Model):
    """
    This mixin introduces two new fields that store the created on and updated on
    timestamps.
    """

    created_on = models.DateTimeField(auto_now_add=True, blank=True, editable=False)
    updated_on = models.DateTimeField(auto_now=True, blank=True, editable=False)

    class Meta:
        abstract = True


def make_trashable_mixin(parent):
    """
    Constructs a mixin class which overrides a models managers to ensure trashed entries
    are not available via objects, but instead via the new trash manager.

    :param parent: If specified will use the trashed column in a related model where
        parent is the name of the FK to the related model.
    :return: A mixin with overridden managers which correctly filter out trashed rows.
    """

    no_trash_manager = make_trash_manager(trashed=False, parent=parent)
    trash_only_manager = make_trash_manager(trashed=True, parent=parent)

    class TrashableMixin(models.Model):
        objects = no_trash_manager()
        trash = trash_only_manager()
        objects_and_trash = Manager()

        class Meta:
            abstract = True

    return TrashableMixin


ParentGroupTrashableModelMixin = make_trashable_mixin("group")


class TrashableModelMixin(models.Model):
    """
    This mixin allows this model to be trashed and restored from the trash by adding
    new columns recording it's trash status.
    """

    trashed = models.BooleanField(default=False, db_index=True)

    objects = NoTrashManager()
    trash = TrashOnlyManager()
    objects_and_trash = Manager()

    class Meta:
        abstract = True
