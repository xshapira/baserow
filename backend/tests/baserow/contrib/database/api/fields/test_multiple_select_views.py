import pytest
from django.shortcuts import reverse
from rest_framework.status import (
    HTTP_200_OK,
    HTTP_400_BAD_REQUEST,
)
from baserow.contrib.database.fields.models import SelectOption
from baserow.test_utils.helpers import is_dict_subset


@pytest.mark.django_db
@pytest.mark.field_multiple_select
@pytest.mark.api_rows
def test_batch_create_rows_multiple_select_field(api_client, data_fixture):
    user, jwt_token = data_fixture.create_user_and_token()
    table = data_fixture.create_database_table(user=user)
    multiple_select_field = data_fixture.create_multiple_select_field(table=table)
    select_option_1 = SelectOption.objects.create(
        field=multiple_select_field,
        order=1,
        value="Option 1",
        color="blue",
    )
    select_option_2 = SelectOption.objects.create(
        field=multiple_select_field,
        order=1,
        value="Option 2",
        color="blue",
    )
    select_option_3 = SelectOption.objects.create(
        field=multiple_select_field,
        order=1,
        value="Option 3",
        color="blue",
    )
    multiple_select_field.select_options.set([select_option_1, select_option_2])
    model = table.get_model()
    url = reverse("api:database:rows:batch", kwargs={"table_id": table.id})
    request_body = {
        "items": [
            {
                f"field_{multiple_select_field.id}": [select_option_3.id],
            },
            {
                f"field_{multiple_select_field.id}": [
                    select_option_3.id,
                    select_option_2.id,
                ],
            },
            {
                f"field_{multiple_select_field.id}": [],
            },
        ]
    }
    expected_response_body = {
        "items": [
            {
                "id": 1,
                f"field_{multiple_select_field.id}": [
                    {
                        "id": select_option_3.id,
                        "color": "blue",
                        "value": "Option 3",
                    }
                ],
                "order": "1.00000000000000000000",
            },
            {
                "id": 2,
                f"field_{multiple_select_field.id}": [
                    {
                        "id": select_option_3.id,
                        "color": "blue",
                        "value": "Option 3",
                    },
                    {
                        "id": select_option_2.id,
                        "color": "blue",
                        "value": "Option 2",
                    },
                ],
                "order": "2.00000000000000000000",
            },
            {
                "id": 3,
                f"field_{multiple_select_field.id}": [],
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
    assert getattr(rows[0], f"field_{multiple_select_field.id}").count() == 1
    assert getattr(rows[1], f"field_{multiple_select_field.id}").count() == 2
    assert getattr(rows[2], f"field_{multiple_select_field.id}").count() == 0


@pytest.mark.django_db
@pytest.mark.field_multiple_select
@pytest.mark.api_rows
def test_batch_update_rows_multiple_select_field(api_client, data_fixture):
    user, jwt_token = data_fixture.create_user_and_token()
    table = data_fixture.create_database_table(user=user)
    multiple_select_field = data_fixture.create_multiple_select_field(table=table)
    select_option_1 = SelectOption.objects.create(
        field=multiple_select_field,
        order=1,
        value="Option 1",
        color="blue",
    )
    select_option_2 = SelectOption.objects.create(
        field=multiple_select_field,
        order=1,
        value="Option 2",
        color="blue",
    )
    select_option_3 = SelectOption.objects.create(
        field=multiple_select_field,
        order=1,
        value="Option 3",
        color="blue",
    )
    multiple_select_field.select_options.set([select_option_1, select_option_2])
    model = table.get_model()
    row_1 = model.objects.create()
    row_2 = model.objects.create()
    row_3 = model.objects.create()
    url = reverse("api:database:rows:batch", kwargs={"table_id": table.id})
    request_body = {
        "items": [
            {
                "id": row_1.id,
                f"field_{multiple_select_field.id}": [select_option_3.id],
            },
            {
                "id": row_2.id,
                f"field_{multiple_select_field.id}": [
                    select_option_3.id,
                    select_option_2.id,
                ],
            },
            {"id": row_3.id, f"field_{multiple_select_field.id}": []},
        ]
    }

    expected_response_body = {
        "items": [
            {
                "id": row_1.id,
                f"field_{multiple_select_field.id}": [
                    {
                        "id": select_option_3.id,
                        "color": "blue",
                        "value": "Option 3",
                    }
                ],
                "order": "1.00000000000000000000",
            },
            {
                "id": row_2.id,
                f"field_{multiple_select_field.id}": [
                    {
                        "id": select_option_3.id,
                        "color": "blue",
                        "value": "Option 3",
                    },
                    {
                        "id": select_option_2.id,
                        "color": "blue",
                        "value": "Option 2",
                    },
                ],
                "order": "1.00000000000000000000",
            },
            {
                "id": row_3.id,
                f"field_{multiple_select_field.id}": [],
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
    assert getattr(row_1, f"field_{multiple_select_field.id}").count() == 1
    assert getattr(row_2, f"field_{multiple_select_field.id}").count() == 2
    assert getattr(row_3, f"field_{multiple_select_field.id}").count() == 0


@pytest.mark.django_db
@pytest.mark.field_multiple_select
@pytest.mark.api_rows
def test_batch_update_rows_multiple_select_field_wrong_option(api_client, data_fixture):
    user, jwt_token = data_fixture.create_user_and_token()
    table = data_fixture.create_database_table(user=user)
    multiple_select_field = data_fixture.create_multiple_select_field(table=table)
    select_option_1 = SelectOption.objects.create(
        field=multiple_select_field,
        order=1,
        value="Option 1",
        color="blue",
    )
    multiple_select_field.select_options.set([select_option_1])
    model = table.get_model()
    row_1 = model.objects.create()
    row_2 = model.objects.create()
    url = reverse("api:database:rows:batch", kwargs={"table_id": table.id})
    request_body = {
        "items": [
            {"id": row_1.id, f"field_{multiple_select_field.id}": [787]},
            {
                "id": row_2.id,
                f"field_{multiple_select_field.id}": [789, select_option_1.id],
            },
        ]
    }


    response = api_client.patch(
        url,
        request_body,
        format="json",
        HTTP_AUTHORIZATION=f"JWT {jwt_token}",
    )

    assert response.status_code == HTTP_400_BAD_REQUEST
    assert response.json()["error"] == "ERROR_INVALID_SELECT_OPTION_VALUES"
    assert (
        response.json()["detail"]
        == "The provided select option ids [787, 789] are not valid select options."
    )


@pytest.mark.django_db
@pytest.mark.field_multiple_select
@pytest.mark.api_rows
def test_batch_update_rows_multiple_select_field_null_as_id(api_client, data_fixture):
    user, jwt_token = data_fixture.create_user_and_token()
    table = data_fixture.create_database_table(user=user)
    multiple_select_field = data_fixture.create_multiple_select_field(table=table)
    select_option_1 = SelectOption.objects.create(
        field=multiple_select_field,
        order=1,
        value="Option 1",
        color="blue",
    )
    multiple_select_field.select_options.set([select_option_1])
    model = table.get_model()
    row_1 = model.objects.create()
    url = reverse("api:database:rows:batch", kwargs={"table_id": table.id})
    request_body = {
        "items": [
            {"id": row_1.id, f"field_{multiple_select_field.id}": [None]}
        ]
    }


    response = api_client.patch(
        url,
        request_body,
        format="json",
        HTTP_AUTHORIZATION=f"JWT {jwt_token}",
    )

    assert response.status_code == HTTP_400_BAD_REQUEST
    assert response.json()["error"] == "ERROR_INVALID_SELECT_OPTION_VALUES"
    assert (
        response.json()["detail"]
        == "The provided select option ids [None] are not valid select options."
    )


@pytest.mark.django_db
@pytest.mark.field_multiple_select
@pytest.mark.api_rows
def test_batch_update_rows_multiple_select_field_maintain_relationships(
    api_client, data_fixture
):
    """
    Since we are deleting and recreating m2m relationships we want to be sure
    that already existing relationships are preserved
    """

    user, jwt_token = data_fixture.create_user_and_token()
    table = data_fixture.create_database_table(user=user)
    multiple_select_field = data_fixture.create_multiple_select_field(table=table)
    # first select field
    select_option_1 = SelectOption.objects.create(
        field=multiple_select_field,
        order=1,
        value="Option 1",
        color="blue",
    )
    select_option_2 = SelectOption.objects.create(
        field=multiple_select_field,
        order=1,
        value="Option 2",
        color="blue",
    )
    multiple_select_field.select_options.set([select_option_1, select_option_2])
    multiple_select_field_b = data_fixture.create_multiple_select_field(table=table)
    # second select field
    select_option_b_1 = SelectOption.objects.create(
        field=multiple_select_field_b,
        order=1,
        value="Option 1",
        color="blue",
    )
    select_option_b_2 = SelectOption.objects.create(
        field=multiple_select_field_b,
        order=1,
        value="Option 2",
        color="blue",
    )
    multiple_select_field.select_options.set([select_option_b_1, select_option_b_2])
    model = table.get_model()
    # store some data beforehand
    row_1 = model.objects.create()
    getattr(row_1, f"field_{multiple_select_field.id}").set([select_option_1.id])
    getattr(row_1, f"field_{multiple_select_field_b.id}").set([select_option_b_2.id])
    row_1.save()
    row_2 = model.objects.create()
    getattr(row_2, f"field_{multiple_select_field.id}").set(
        [select_option_1.id, select_option_2.id]
    )
    getattr(row_2, f"field_{multiple_select_field_b.id}").set(
        [select_option_1.id, select_option_2.id]
    )
    row_2.save()

    url = reverse("api:database:rows:batch", kwargs={"table_id": table.id})
    request_body = {
        "items": [{"id": row_2.id, f"field_{multiple_select_field.id}": []}]
    }

    expected_response_body = {
        "items": [{"id": row_2.id, f"field_{multiple_select_field.id}": []}]
    }


    response = api_client.patch(
        url,
        request_body,
        format="json",
        HTTP_AUTHORIZATION=f"JWT {jwt_token}",
    )

    assert response.status_code == HTTP_200_OK
    assert is_dict_subset(expected_response_body, response.json())
    row_1.refresh_from_db()
    row_2.refresh_from_db()
    assert getattr(row_1, f"field_{multiple_select_field.id}").count() == 1
    assert getattr(row_2, f"field_{multiple_select_field.id}").count() == 0
    assert getattr(row_1, f"field_{multiple_select_field_b.id}").count() == 1
    assert getattr(row_2, f"field_{multiple_select_field_b.id}").count() == 2
