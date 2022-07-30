import re
import json
import requests
from pytz import UTC, BaseTzInfo, timezone as pytz_timezone
from collections import defaultdict
from typing import List, Tuple, Union, Dict, Optional
from requests import Response
from io import BytesIO, IOBase
from zipfile import ZipFile, ZIP_DEFLATED
from datetime import datetime

from django.core.files.storage import Storage
from django.contrib.auth import get_user_model
from django.db import transaction

from baserow.core.handler import CoreHandler
from baserow.core.utils import (
    remove_invalid_surrogate_characters,
    ChildProgressBuilder,
)
from baserow.core.models import Group
from baserow.core.export_serialized import CoreExportSerializedStructure
from baserow.contrib.database.export_serialized import DatabaseExportSerializedStructure
from baserow.contrib.database.models import Database
from baserow.contrib.database.fields.models import Field
from baserow.contrib.database.fields.field_types import FieldType, field_type_registry
from baserow.contrib.database.views.registries import view_type_registry
from baserow.contrib.database.views.models import GridView
from baserow.contrib.database.application_types import DatabaseApplicationType
from baserow.contrib.database.airtable.registry import (
    AirtableColumnType,
    airtable_column_type_registry,
)
from baserow.contrib.database.airtable.constants import (
    AIRTABLE_EXPORT_JOB_DOWNLOADING_BASE,
    AIRTABLE_EXPORT_JOB_DOWNLOADING_FILES,
    AIRTABLE_EXPORT_JOB_CONVERTING,
)

from .exceptions import (
    AirtableBaseNotPublic,
    AirtableShareIsNotABase,
    AirtableImportJobDoesNotExist,
    AirtableImportJobAlreadyRunning,
)
from .models import AirtableImportJob
from .tasks import run_import_from_airtable


User = get_user_model()


BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:95.0) Gecko/20100101 Firefox/95.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "cross-site",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
}


class AirtableHandler:
    @staticmethod
    def fetch_publicly_shared_base(share_id: str) -> Tuple[str, dict, dict]:
        """
        Fetches the initial page of the publicly shared page. It will parse the content
        and extract and return the initial data needed for future requests.

        :param share_id: The Airtable share id of the page that must be fetched. Note
            that the base must be shared publicly. The id stars with `shr`.
        :return: The request ID, initial data and the cookies of the response.
        """

        url = f"https://airtable.com/{share_id}"
        response = requests.get(url, headers=BASE_HEADERS)

        if not response.ok:
            raise AirtableBaseNotPublic(
                f"The base with share id {share_id} is not public."
            )

        decoded_content = remove_invalid_surrogate_characters(response.content)

        request_id = re.search('requestId: "(.*)",', decoded_content)[1]
        raw_init_data = re.search("window.initData = (.*);\n", decoded_content)[1]
        init_data = json.loads(raw_init_data)
        cookies = response.cookies.get_dict()

        if "sharedApplicationId" not in raw_init_data:
            raise AirtableShareIsNotABase("The `shared_id` is not a base.")

        return request_id, init_data, cookies

    @staticmethod
    def fetch_table_data(
        table_id: str,
        init_data: dict,
        request_id: str,
        cookies: dict,
        fetch_application_structure: bool,
        stream=True,
    ) -> Response:
        """
        Fetches the data or application structure of a publicly shared Airtable table.

        :param table_id: The Airtable table id that must be fetched. The id starts with
            `tbl`.
        :param init_data: The init_data returned by the initially requested shared base.
        :param request_id: The request_id returned by the initially requested shared
            base.
        :param cookies: The cookies dict returned by the initially requested shared
            base.
        :param fetch_application_structure: Indicates whether the application structure
            must also be fetched. If True, the schema of all the tables and views will
            be included in the response. Note that the structure of the response is
            different because it will wrap application/table schema around the table
            data. The table data will be available at the path `data.tableDatas.0.rows`.
            If False, the only the table data will be included in the response JSON,
            which will be available at the path `data.rows`.
        :param stream: Indicates whether the request should be streamed. This could be
            useful if we want to show a progress bar. It will directly be passed into
            the `requests` request.
        :return: The `requests` response containing the result.
        """

        application_id = list(init_data["rawApplications"].keys())[0]
        client_code_version = init_data["codeVersion"]
        page_load_id = init_data["pageLoadId"]

        stringified_object_params = {
            "includeDataForViewIds": None,
            "shouldIncludeSchemaChecksum": True,
            "mayOnlyIncludeRowAndCellDataForIncludedViews": False,
        }
        access_policy = json.loads(init_data["accessPolicy"])

        if fetch_application_structure:
            stringified_object_params["includeDataForTableIds"] = [table_id]
            url = f"https://airtable.com/v0.3/application/{application_id}/read"
        else:
            url = f"https://airtable.com/v0.3/table/{table_id}/readData"

        return requests.get(
            url=url,
            stream=stream,
            params={
                "stringifiedObjectParams": json.dumps(stringified_object_params),
                "requestId": request_id,
                "accessPolicy": json.dumps(access_policy),
            },
            headers={
                "x-airtable-application-id": application_id,
                "x-airtable-client-queue-time": "45",
                "x-airtable-inter-service-client": "webClient",
                "x-airtable-inter-service-client-code-version": client_code_version,
                "x-airtable-page-load-id": page_load_id,
                "X-Requested-With": "XMLHttpRequest",
                "x-time-zone": "Europe/Amsterdam",
                "x-user-locale": "en",
                **BASE_HEADERS,
            },
            cookies=cookies,
        )

    @staticmethod
    def extract_schema(exports: List[dict]) -> Tuple[dict, dict]:
        """
        Loops over the provided exports and finds the export containing the application
        schema. That will be extracted and the rest of the table data will be moved
        into a dict where the key is the table id.

        :param exports: A list containing all the exports as dicts.
        :return: The database schema and a dict containing the table data.
        """

        schema = None
        tables = {}

        for export in exports:
            if "appBlanket" in export["data"]:
                table_data = export["data"].pop("tableDatas")[0]
                schema = export["data"]
            else:
                table_data = export["data"]

            tables[table_data["id"]] = table_data

        if schema is None:
            raise ValueError("None of the provided exports contains the schema.")

        return schema, tables

    @staticmethod
    def to_baserow_field(
        table: dict,
        column: dict,
        timezone: BaseTzInfo,
    ) -> Union[Tuple[None, None, None], Tuple[Field, FieldType, AirtableColumnType]]:
        """
        Converts the provided Airtable column dict to the righ a Baserow field object.

        :param table: The Airtable table dict. This is needed to figure out whether the
            field is the primary field.
        :param column: The Airtable column dict. These values will be converted to
            Baserow format.
        :param timezone: The main timezone used for date conversions if needed.
        :return: The converted Baserow field, field type and the Airtable column type.
        """

        (
            baserow_field,
            airtable_column_type,
        ) = airtable_column_type_registry.from_airtable_column_to_serialized(
            table, column, timezone
        )

        if baserow_field is None:
            return None, None, None

        baserow_field_type = field_type_registry.get_by_model(baserow_field)

        try:
            order = next(
                index
                for index, value in enumerate(table["meaningfulColumnOrder"])
                if value["columnId"] == column["id"]
            )
        except StopIteration:
            order = 32767

        baserow_field.id = column["id"]
        baserow_field.name = column["name"]
        baserow_field.order = order
        baserow_field.primary = (
            baserow_field_type.can_be_primary_field
            and table["primaryColumnId"] == column["id"]
        )

        return baserow_field, baserow_field_type, airtable_column_type

    @staticmethod
    def to_baserow_row_export(
        row_id_mapping: Dict[str, Dict[str, int]],
        column_mapping: Dict[str, dict],
        row: dict,
        index: int,
        timezone: BaseTzInfo,
        files_to_download: Dict[str, str],
    ) -> dict:
        """
        Converts the provided Airtable record to a Baserow row by looping over the field
        types and executing the `from_airtable_column_value_to_serialized` method.

        :param row_id_mapping: A mapping containing the table as key as the value is
            another mapping where the Airtable row id maps the Baserow row id.
        :param column_mapping: A mapping where the Airtable colum id is the value and
            the value containing another mapping with the Airtable column dict and
            Baserow field dict.
        :param row: The Airtable row that must be converted a Baserow row.
        :param index: The index the row has in the table.
        :param timezone: The main timezone used for date conversions if needed.
        :param files_to_download: A dict that contains all the user file URLs that must
            be downloaded. The key is the file name and the value the URL. Additional
            files can be added to this dict.
        :return: The converted row in Baserow export format.
        """

        created_on = row.get("createdTime")

        if created_on:
            created_on = (
                datetime.strptime(created_on, "%Y-%m-%dT%H:%M:%S.%fZ")
                .replace(tzinfo=UTC)
                .isoformat()
            )

        exported_row = DatabaseExportSerializedStructure.row(
            id=row["id"],
            order=f"{index + 1}.00000000000000000000",
            created_on=created_on,
            updated_on=None,
        )

        # Some empty rows don't have the `cellValuesByColumnId` property because it
        # doesn't contain values, hence the fallback to prevent failing hard.
        cell_values = row.get("cellValuesByColumnId", {})
        for column_id, column_value in cell_values.items():
            if column_id not in column_mapping:
                continue

            mapping_values = column_mapping[column_id]
            baserow_serialized_value = mapping_values[
                "airtable_column_type"
            ].to_baserow_export_serialized_value(
                row_id_mapping,
                mapping_values["raw_airtable_column"],
                mapping_values["baserow_field"],
                column_value,
                timezone,
                files_to_download,
            )
            exported_row[f"field_{column_id}"] = baserow_serialized_value

        return exported_row

    @staticmethod
    def download_files_as_zip(
        files_to_download: Dict[str, str],
        progress_builder: Optional[ChildProgressBuilder] = None,
        files_buffer: Union[None, IOBase] = None,
    ) -> BytesIO:
        """
        Downloads all the user files in the provided dict and adds them to a zip file.
        The key of the dict will be the file name in the zip file.

        :param files_to_download: A dict that contains all the user file URLs that must
            be downloaded. The key is the file name and the value the URL. Additional
            files can be added to this dict.
        :param progress_builder: If provided will be used to build a child progress bar
            and report on this methods progress to the parent of the progress_builder.
        :param files_buffer: Optionally a file buffer can be provided to store the
            downloaded files in. They will be stored in memory if not provided.
        :return: An in memory buffer as zip file containing all the user files.
        """

        if files_buffer is None:
            files_buffer = BytesIO()

        progress = ChildProgressBuilder.build(
            progress_builder, child_total=len(files_to_download.keys())
        )

        with ZipFile(files_buffer, "a", ZIP_DEFLATED, False) as files_zip:
            for file_name, url in files_to_download.items():
                response = requests.get(url, headers=BASE_HEADERS)
                files_zip.writestr(file_name, response.content)
                progress.increment(state=AIRTABLE_EXPORT_JOB_DOWNLOADING_FILES)

        return files_buffer

    @classmethod
    def to_baserow_database_export(
        cls,
        init_data: dict,
        schema: dict,
        tables: list,
        timezone: BaseTzInfo,
        progress_builder: Optional[ChildProgressBuilder] = None,
        download_files_buffer: Union[None, IOBase] = None,
    ) -> Tuple[dict, IOBase]:
        """
        Converts the provided raw Airtable database dict to a Baserow export format and
        an in memory zip file containing all the downloaded user files.

        @TODO add the views.
        @TODO preserve the order of least one view.

        :param init_data: The init_data, extracted from the initial page related to the
            shared base.
        :param schema: An object containing the schema of the Airtable base.
        :param tables: a list containing the table data.
        :param timezone: The main timezone used for date conversions if needed.
        :param progress_builder: If provided will be used to build a child progress bar
            and report on this methods progress to the parent of the progress_builder.
        :param download_files_buffer: Optionally a file buffer can be provided to store
            the downloaded files in. They will be stored in memory if not provided.
        :return: The converted Airtable base in Baserow export format and a zip file
            containing the user files.
        """

        progress = ChildProgressBuilder.build(progress_builder, child_total=1000)
        converting_progress = progress.create_child(
            represents_progress=500,
            total=sum(
                len(tables[table["id"]]["rows"])
                # Table column progress
                + len(table["columns"])
                # Table rows progress
                + len(tables[table["id"]]["rows"])
                # The table itself.
                + 1
                for table in schema["tableSchemas"]
            ),
        )


        # A list containing all the exported table in Baserow format.
        exported_tables = []

        # A dict containing all the user files that must be downloaded and added to a
        # zip file.
        files_to_download = {}

        # A mapping containing the Airtable table id as key and as value another mapping
        # containing with the key as Airtable row id and the value as new Baserow row
        # id. This mapping is created because Airtable has string row id that look like
        # "recAjnk3nkj5", but Baserow doesn't support string row id, so we need to
        # replace them with a unique int. We need a mapping because there could be
        # references to the row.
        row_id_mapping = defaultdict(dict)
        for table in schema["tableSchemas"]:
            for row_index, row in enumerate(tables[table["id"]]["rows"]):
                new_id = row_index + 1
                row_id_mapping[table["id"]][row["id"]] = new_id
                row["id"] = new_id
                converting_progress.increment(state=AIRTABLE_EXPORT_JOB_CONVERTING)

        view_id = 0
        for table_index, table in enumerate(schema["tableSchemas"]):
            field_mapping = {}

            # Loop over all the columns in the table and try to convert them to Baserow
            # format.
            primary = None
            for column in table["columns"]:
                (
                    baserow_field,
                    baserow_field_type,
                    airtable_column_type,
                ) = cls.to_baserow_field(table, column, timezone)
                converting_progress.increment(state=AIRTABLE_EXPORT_JOB_CONVERTING)

                # None means that none of the field types know how to parse this field,
                # so we must ignore it.
                if baserow_field is None:
                    continue

                # Construct a mapping where the Airtable column id is the key and the
                # value contains the raw Airtable column values, Baserow field and
                # the Baserow field type object for later use.
                field_mapping[column["id"]] = {
                    "baserow_field": baserow_field,
                    "baserow_field_type": baserow_field_type,
                    "raw_airtable_column": column,
                    "airtable_column_type": airtable_column_type,
                }
                if baserow_field.primary:
                    primary = baserow_field

            if primary is None:
                # First check if another field can act as the primary field type.
                found_existing_field = False
                for value in field_mapping.values():
                    if field_type_registry.get_by_model(
                        value["baserow_field"]
                    ).can_be_primary_field:
                        value["baserow_field"].primary = True
                        found_existing_field = True
                        break

                # If none of the existing fields can be primary, we will add a new
                # text field.
                if not found_existing_field:
                    airtable_column = {
                        "id": "primary_field",
                        "name": "Primary field (auto created)",
                        "type": "text",
                    }
                    (
                        baserow_field,
                        baserow_field_type,
                        airtable_column_type,
                    ) = cls.to_baserow_field(table, airtable_column, timezone)
                    baserow_field.primary = True
                    field_mapping["primary_id"] = {
                        "baserow_field": baserow_field,
                        "baserow_field_type": baserow_field_type,
                        "raw_airtable_column": airtable_column,
                        "airtable_column_type": airtable_column_type,
                    }

            # Loop over all the fields and convert them to Baserow serialized format.
            exported_fields = [
                value["baserow_field_type"].export_serialized(value["baserow_field"])
                for value in field_mapping.values()
            ]

            # Loop over all the rows in the table and convert them to Baserow format. We
            # need to provide the `row_id_mapping` and `field_mapping` because there
            # could be references to other rows and fields. the `files_to_download` is
            # needed because every value could be depending on additional files that
            # must later be downloaded.
            exported_rows = []
            for row_index, row in enumerate(tables[table["id"]]["rows"]):
                exported_rows.append(
                    cls.to_baserow_row_export(
                        row_id_mapping,
                        field_mapping,
                        row,
                        row_index,
                        timezone,
                        files_to_download,
                    )
                )
                converting_progress.increment(state=AIRTABLE_EXPORT_JOB_CONVERTING)

            # Create a default grid view because the importing of views doesn't work
            # yet. It's a bit quick and dirty, but it will be replaced soon.
            view_id += 1
            grid_view = GridView(id=view_id, name="Grid", order=1)
            grid_view.get_field_options = lambda *args, **kwargs: []
            grid_view_type = view_type_registry.get_by_model(grid_view)
            exported_views = [grid_view_type.export_serialized(grid_view, None, None)]

            exported_table = DatabaseExportSerializedStructure.table(
                id=table["id"],
                name=table["name"],
                order=table_index,
                fields=exported_fields,
                views=exported_views,
                rows=exported_rows,
            )
            exported_tables.append(exported_table)
            converting_progress.increment(state=AIRTABLE_EXPORT_JOB_CONVERTING)

        exported_database = CoreExportSerializedStructure.application(
            id=1,
            name=init_data["rawApplications"][init_data["sharedApplicationId"]]["name"],
            order=1,
            type=DatabaseApplicationType.type,
        )
        exported_database.update(
            **DatabaseExportSerializedStructure.database(tables=exported_tables)
        )

        # After all the tables have been converted to Baserow format, we can must
        # download all the user files. Because we first want to the whole conversion to
        # be completed and because we want this to be added to the progress bar, this is
        # done last.
        user_files_zip = cls.download_files_as_zip(
            files_to_download,
            progress.create_child_builder(represents_progress=500),
            download_files_buffer,
        )

        return exported_database, user_files_zip

    @classmethod
    def import_from_airtable_to_group(
        cls,
        group: Group,
        share_id: str,
        timezone: BaseTzInfo = UTC,
        storage: Optional[Storage] = None,
        progress_builder: Optional[ChildProgressBuilder] = None,
        download_files_buffer: Union[None, IOBase] = None,
    ) -> Tuple[List[Database], dict]:
        """
        Downloads all the data of the provided publicly shared Airtable base, converts
        it into Baserow export format, downloads the related files and imports that
        converted base into the provided group.

        :param group: The group where the copy of the Airtable must be added to.
        :param share_id: The shared Airtable ID that must be imported.
        :param timezone: The main timezone used for date conversions if needed.
        :param storage: The storage where the user files must be saved to.
        :param progress_builder: If provided will be used to build a child progress bar
            and report on this methods progress to the parent of the progress_builder.
        :param download_files_buffer: Optionally a file buffer can be provided to store
            the downloaded files in. They will be stored in memory if not provided.
        :return: The imported database application representing the Airtable base.
        """

        progress = ChildProgressBuilder.build(progress_builder, child_total=1000)

        # Execute the initial request to obtain the initial data that's needed to
        # make the request.
        request_id, init_data, cookies = cls.fetch_publicly_shared_base(share_id)
        progress.increment(state=AIRTABLE_EXPORT_JOB_DOWNLOADING_BASE)

        # Loop over all the tables and make a request for each table to obtain the raw
        # Airtable table data.
        tables = []
        raw_tables = list(init_data["rawTables"].keys())
        for index, table_id in enumerate(
            progress.track(
                represents_progress=99,
                state=AIRTABLE_EXPORT_JOB_DOWNLOADING_BASE,
                iterable=raw_tables,
            )
        ):
            response = cls.fetch_table_data(
                table_id=table_id,
                init_data=init_data,
                request_id=request_id,
                cookies=cookies,
                # At least one request must also fetch the application structure that
                # contains the schema of all the tables, so we do this for the first
                # table.
                fetch_application_structure=index == 0,
                stream=False,
            )
            decoded_content = remove_invalid_surrogate_characters(response.content)
            tables.append(json.loads(decoded_content))

        # Split database schema from the tables because we need this to be separated
        # later on..
        schema, tables = cls.extract_schema(tables)

        # Convert the raw Airtable data to Baserow export format so we can import that
        # later.
        baserow_database_export, files_buffer = cls.to_baserow_database_export(
            init_data,
            schema,
            tables,
            timezone,
            progress.create_child_builder(represents_progress=300),
            download_files_buffer,
        )

        # Import the converted data using the existing method to avoid duplicate code.
        databases, id_mapping = CoreHandler().import_applications_to_group(
            group,
            [baserow_database_export],
            files_buffer,
            storage=storage,
            progress_builder=progress.create_child_builder(represents_progress=600),
        )

        return databases, id_mapping

    @staticmethod
    def get_airtable_import_job(user: User, job_id: int) -> AirtableImportJob:
        """
        Fetches an Airtable import job from the database if the user has created it.
        The properties like `progress_percentage` and `progress_state` are
        automatically updated by the task that does the actual import.

        :param user: The user on whose behalf the job is requested.
        :param job_id: The id of the job that must be fetched.
        :raises AirtableImportJobDoesNotExist: If the import job doesn't exist.
        :return: The fetched Airtable import job instance related to the provided id.
        """

        try:
            return AirtableImportJob.objects.select_related(
                "user", "group", "database", "database__group"
            ).get(id=job_id, user_id=user.id)
        except AirtableImportJob.DoesNotExist:
            raise AirtableImportJobDoesNotExist(
                f"The job with id {job_id} does not exist."
            )

    @staticmethod
    def create_and_start_airtable_import_job(
        user: User,
        group: Group,
        share_id: str,
        timezone: Optional[str] = None,
    ) -> AirtableImportJob:
        """
        Creates a new Airtable import jobs and starts the asynchronous task that
        actually does the import.

        :param user: The user on whose behalf the import is started.
        :param group: The group where the Airtable base be imported to.
        :param share_id: The Airtable share id of the page that must be fetched. Note
            that the base must be shared publicly. The id stars with `shr`.
        :param timezone: The main timezone used for date conversions if needed.
        :raises AirtableImportJobAlreadyRunning: If another import job is already
            running. A user can only have one job running simultaneously.
        :raises UnknownTimeZoneError: When the provided timezone string is incorrect.
        :return: The newly created Airtable import job.
        """

        # Validate the provided timezone.
        if timezone is not None:
            pytz_timezone(timezone)

        group.has_user(user, raise_error=True)

        # A user can only have one Airtable import job running simultaneously. If one
        # is already running, we don't want to start a new one.
        running_jobs = AirtableImportJob.objects.filter(user_id=user.id).is_running()
        if len(running_jobs) > 0:
            raise AirtableImportJobAlreadyRunning(
                f"Another job is already running with id {running_jobs[0].id}."
            )

        job = AirtableImportJob.objects.create(
            user=user,
            group=group,
            airtable_share_id=share_id,
            timezone=timezone,
        )
        transaction.on_commit(lambda: run_import_from_airtable.delay(job.id))
        return job
