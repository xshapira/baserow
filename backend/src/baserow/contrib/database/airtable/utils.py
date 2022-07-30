import re


def extract_share_id_from_url(public_base_url: str) -> str:
    """
    Extracts the Airtable share id from the provided URL.

    :param public_base_url: The URL where the share id must be extracted from.
    :raises ValueError: If the provided URL doesn't match the publicly shared
        Airtable URL.
    :return: The extracted share id.
    """

    if result := re.search(
        r"https:\/\/airtable.com\/shr(.*)$", public_base_url
    ):
        return f"shr{result[1]}"
    else:
        raise ValueError(
            'Please provide a valid shared Airtable URL (e.g. https://airtable.com/shrxxxxxxxxxxxxxx)'
        )
