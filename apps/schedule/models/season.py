"""Model for a season."""

from datetime import date

from bg_uuidv7 import uuidv7
from django.db import models


class Season(models.Model):
    """Model for a season."""

    id_uuid = models.UUIDField[str, str](
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    name = models.CharField[str, str](max_length=255, unique=True)
    start_date = models.DateField[date, date]()
    end_date = models.DateField[date, date]()

    def __str__(self) -> str:
        """Get the string representation of the season.

        Returns:
            str: The name of the season.

        """
        return str(self.name)
