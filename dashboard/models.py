from django.db import models


class ElectionPoint(models.Model):
    """One durable observation of official votes and its resulting projection."""

    office = models.CharField(max_length=32, db_index=True)
    turn = models.PositiveSmallIntegerField()
    election_id = models.CharField(max_length=16)
    signature = models.CharField(max_length=64, unique=True)
    progress = models.FloatField()
    valid_votes = models.BigIntegerField()
    state_points = models.JSONField(default=list)
    observed = models.JSONField(default=dict)
    forecast = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["office", "turn", "election_id", "id"]) ]
