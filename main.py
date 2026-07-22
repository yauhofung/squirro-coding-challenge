import argparse
import logging
from typing import Any

"""
Skeleton for Squirro Delivery Hiring Coding Challenge
November 2025
"""


log = logging.getLogger(__name__)


def flatten_dict(obj: Any, parent_key: str = "", sep: str = ".") -> dict[str, Any]:
    """Flatten nested dicts/lists into a single-level dict with dotted keys.

    Nested dictionary keys are joined with ``sep`` ("headline.main"), list
    elements are addressed by their index ("keywords.0.value") so that every
    element of the original document is preserved. Empty dicts/lists are kept
    as plain values so their keys don't disappear.
    """
    items: dict[str, Any] = {}
    if isinstance(obj, dict) and obj:
        for key, value in obj.items():
            new_key = f"{parent_key}{sep}{key}" if parent_key else str(key)
            items.update(flatten_dict(value, new_key, sep))
    elif isinstance(obj, list) and obj:
        for index, value in enumerate(obj):
            new_key = f"{parent_key}{sep}{index}" if parent_key else str(index)
            items.update(flatten_dict(value, new_key, sep))
    else:
        items[parent_key] = obj
    return items


class NYTimesSource(object):
    """
    A data loader plugin for the NY Times API.
    """

    def __init__(self):
        pass

    def connect(self, inc_column=None, max_inc_value=None):
        """Connect to the source"""
        log.debug("Incremental Column: %r", inc_column)
        log.debug("Incremental Last Value: %r", max_inc_value)

    def disconnect(self):
        """Disconnect from the source."""
        # Nothing to do
        pass

    def getDataBatch(self, batch_size):
        """
        Generator - Get data from source on batches.

        :returns One list for each batch. Each of those is a list of
                 dictionaries with the defined rows.
        """
        # TODO: implement - this dummy implementation returns one batch of data
        yield [
            {
                "headline.main": "The main headline",
                "_id": "1234",
            }
        ]

    def getSchema(self):
        """
        Return the schema of the dataset
        :returns a List containing the names of the columns retrieved from the
        source
        """

        schema = [
            "title",
            "body",
            "created_at",
            "id",
            "summary",
            "abstract",
            "keywords",
        ]

        return schema


if __name__ == "__main__":
    config = {
        "api_key": "NYTIMES_API_KEY",
        "query": "Silicon Valley",
    }
    source = NYTimesSource()

    # This looks like an argparse dependency - but the Namespace class is just
    # a simple way to create an object holding attributes.
    source.args = argparse.Namespace(**config)

    for idx, batch in enumerate(source.getDataBatch(10)):
        print(f"{idx} Batch of {len(batch)} items")
        for item in batch:
            print(f"  - {item['_id']} - {item['headline.main']}")
