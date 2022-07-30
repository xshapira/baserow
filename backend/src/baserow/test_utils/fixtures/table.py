from baserow.contrib.database.db.schema import safe_django_schema_editor
from baserow.contrib.database.fields.handler import FieldHandler
from baserow.contrib.database.table.models import Table


class TableFixtures:
    def create_database_table(self, user=None, create_table=True, **kwargs):
        if "database" not in kwargs:
            kwargs["database"] = self.create_database_application(user=user)

        if "name" not in kwargs:
            kwargs["name"] = self.fake.name()

        if "order" not in kwargs:
            kwargs["order"] = 0

        table = Table.objects.create(**kwargs)

        if create_table:
            with safe_django_schema_editor() as schema_editor:
                schema_editor.create_model(table.get_model())

        return table

    def build_table(self, columns, rows, user=None, **kwargs):
        table = self.create_database_table(user=user, create_table=True, **kwargs)
        fields = []
        for name, field_type in columns:
            kwargs = {}
            if isinstance(field_type, dict):
                kwargs = field_type
                field_type = kwargs.pop("type")
            fields.append(
                getattr(self, f"create_{field_type}_field")(
                    name=name, table=table, **kwargs
                )
            )

        model = table.get_model()

        created_rows = []
        for row in rows:
            kwargs = {f"field_{field.id}": row[i] for i, field in enumerate(fields)}
            created_rows.append(model.objects.create(**kwargs))

        return table, fields, created_rows

    def create_two_linked_tables(self, user=None, **kwargs):
        if user is None:
            user = self.create_user()

        if "database" not in kwargs:
            database = self.create_database_application(user=user)

        table_a = self.create_database_table(database=database, name="table_a")
        table_b = self.create_database_table(database=database, name="table_b")

        self.create_text_field(table=table_a, name="primary", primary=True)
        self.create_text_field(table=table_b, name="primary", primary=True)

        link_field = FieldHandler().create_field(
            user=user,
            table=table_a,
            type_name="link_row",
            name="link",
            link_row_table=table_b,
        )

        return table_a, table_b, link_field
