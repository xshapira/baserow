from django.db.models import Aggregate, FloatField
from django.contrib.postgres.fields import ArrayField

# Adapted from https://github.com/rtidatascience/django-postgres-stats


class Percentile(Aggregate):
    """
    Accepts a numerical field or expression and a list of fractions and
    returns values for each fraction given corresponding to that fraction in
    that expression.
    If *continuous* is True (the default), the value will be interpolated
    between adjacent values if needed. Otherwise, the value will be the first
    input value whose position in the ordering equals or exceeds the
    specified fraction.

    Usage example::
        from django.contrib.postgres.fields import ArrayField
        numbers = [31, 83, 237, 250, 305, 314, 439, 500, 520, 526, 527, 533,
                   540, 612, 831, 854, 857, 904, 928, 973]
        for n in numbers:
            Number.objects.create(n=n)

        results = Number.objects.all().aggregate(
            median=Percentile('n', 0.5)
        assert results['median'] == 526.5

        results = Number.objects.all().aggregate(
            quartiles=Percentile('n', [0.25, 0.5, 0.75])
        )
        assert results['quartiles'] == [311.75, 526.5, 836.75]

        results = Number.objects.all().aggregate(
            quartiles=Percentile('n', [0.25, 0.5, 0.75],
            continuous=False
        ))
        assert results['quartiles'] == [305, 526, 831]
    """

    function = None
    name = "percentile"
    template = "%(function)s(%(percentiles)s) WITHIN GROUP (ORDER BY %(expressions)s)"

    def __init__(self, expression, percentiles, continuous=True, **extra):
        # Do we have multiple values as percentiles
        if isinstance(percentiles, (list, tuple)):
            percentiles = f"array{percentiles}"
            self.return_array = True
        else:
            self.return_array = False

        extra["function"] = "PERCENTILE_CONT" if continuous else "PERCENTILE_DISC"
        super().__init__(expression, percentiles=percentiles, **extra)

    def _resolve_output_field(self):
        return ArrayField(FloatField()) if self.return_array else FloatField()
