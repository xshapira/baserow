import pytest
from django.shortcuts import reverse
from rest_framework.status import (
    HTTP_200_OK,
)

from baserow.contrib.database.fields.handler import FieldHandler


@pytest.mark.django_db
@pytest.mark.field_link_row
@pytest.mark.api_rows
def test_batch_create_rows_link_row_field(api_client, data_fixture):
    user, jwt_token = data_fixture.create_user_and_token()
    table = data_fixture.create_database_table(user=user)
    linked_table = data_fixture.create_database_table(
        user=user, database=table.database
    )
    linked_field = data_fixture.create_text_field(
        primary=True,
        name="Primary",
        table=linked_table,
    )
    linked_model = linked_table.get_model()
    linked_row_1 = linked_model.objects.create(**{f"field_{linked_field.id}": "Row 1"})
    linked_row_2 = linked_model.objects.create(**{f"field_{linked_field.id}": "Row 2"})
    linked_row_3 = linked_model.objects.create(**{f"field_{linked_field.id}": "Row 3"})
    link_field = FieldHandler().create_field(
        user, table, "link_row", link_row_table=linked_table, name="Link"
    )
    model = table.get_model()
    url = reverse("api:database:rows:batch", kwargs={"table_id": table.id})
    request_body = {
        "items": [
            {
                f"field_{link_field.id}": [linked_row_3.id],
            },
            {
                f"field_{link_field.id}": [linked_row_3.id, linked_row_2.id],
            },
            {
                f"field_{link_field.id}": [],
            },
        ]
    }
    expected_response_body = {
        "items": [
            {
                "id": 1,
                f"field_{link_field.id}": [
                    {"id": linked_row_3.id, "value": "Row 3"}
                ],
                "order": "1.00000000000000000000",
            },
            {
                "id": 2,
                f"field_{link_field.id}": [
                    {"id": linked_row_2.id, "value": "Row 2"},
                    {"id": linked_row_3.id, "value": "Row 3"},
                ],
                "order": "2.00000000000000000000",
            },
            {
                "id": 3,
                f"field_{link_field.id}": [],
                "order": "3.00000000000000000000",
            },
        ]
    }


    response = api_client.post(
        url,
        request_body,
        format="json",
        HTTP_AUTHORIZATION=f"JWT {jwt_token}",
    )

    assert response.status_code == HTTP_200_OK
    assert response.json() == expected_response_body
    rows = model.objects.all()
    assert getattr(rows[0], f"field_{link_field.id}").count() == 1
    assert getattr(rows[1], f"field_{link_field.id}").count() == 2
    assert getattr(rows[2], f"field_{link_field.id}").count() == 0


@pytest.mark.django_db
@pytest.mark.field_link_row
@pytest.mark.api_rows
def test_batch_update_rows_link_row_field(api_client, data_fixture):
    user, jwt_token = data_fixture.create_user_and_token()
    table = data_fixture.create_database_table(user=user)
    linked_table = data_fixture.create_database_table(
        user=user, database=table.database
    )
    linked_field = data_fixture.create_text_field(
        primary=True,
        name="Primary",
        table=linked_table,
    )
    linked_model = linked_table.get_model()
    linked_row_1 = linked_model.objects.create(**{f"field_{linked_field.id}": "Row 1"})
    linked_row_2 = linked_model.objects.create(**{f"field_{linked_field.id}": "Row 2"})
    linked_row_3 = linked_model.objects.create(**{f"field_{linked_field.id}": "Row 3"})
    link_field = FieldHandler().create_field(
        user, table, "link_row", link_row_table=linked_table, name="Link"
    )
    model = table.get_model()
    row_1 = model.objects.create()
    row_2 = model.objects.create()
    row_3 = model.objects.create()
    url = reverse("api:database:rows:batch", kwargs={"table_id": table.id})
    request_body = {
        "items": [
            {"id": row_1.id, f"field_{link_field.id}": [linked_row_3.id]},
            {
                "id": row_2.id,
                f"field_{link_field.id}": [linked_row_3.id, linked_row_2.id],
            },
            {"id": row_3.id, f"field_{link_field.id}": []},
        ]
    }

    expected_response_body = {
        "items": [
            {
                "id": row_1.id,
                f"field_{link_field.id}": [
                    {"id": linked_row_3.id, "value": "Row 3"}
                ],
                "order": "1.00000000000000000000",
            },
            {
                "id": row_2.id,
                f"field_{link_field.id}": [
                    {"id": linked_row_2.id, "value": "Row 2"},
                    {"id": linked_row_3.id, "value": "Row 3"},
                ],
                "order": "1.00000000000000000000",
            },
            {
                "id": row_3.id,
                f"field_{link_field.id}": [],
                "order": "1.00000000000000000000",
            },
        ]
    }


    response = api_client.patch(
        url,
        request_body,
        format="json",
        HTTP_AUTHORIZATION=f"JWT {jwt_token}",
    )

    assert response.status_code == HTTP_200_OK
    assert response.json() == expected_response_body
    assert getattr(row_1, f"field_{link_field.id}").count() == 1
    assert getattr(row_2, f"field_{link_field.id}").count() == 2
    assert getattr(row_3, f"field_{link_field.id}").count() == 0
