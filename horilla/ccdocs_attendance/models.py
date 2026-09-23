"""
Floor attendance models.

One store: every notice, day result, point and delivery claim lives here.
Points are never deleted -- a wrong point is voided (voided_at/by/reason) and a
voided point is never brought back by the engine.
"""

from django.db import models
from django.db.models import Q

from employee.models import Employee


class AgentLink(models.Model):
    """Which dialer login (and/or WebWork email) belongs to an employee, and when."""

    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="ccdocs_attendance_links"
    )
    dialer_user = models.CharField(max_length=40, null=True, blank=True)
    webwork_email = models.EmailField(max_length=254, null=True, blank=True)
    valid_from = models.DateField()
    # Last day the link is valid (inclusive). NULL = the link is open.
    valid_to = models.DateField(null=True, blank=True)
    close_reason = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["employee_id", "valid_from", "id"]
        constraints = [
            # At most one OPEN holder per dialer login.
            models.UniqueConstraint(
                fields=["dialer_user"],
                condition=Q(valid_to__isnull=True) & Q(dialer_user__isnull=False),
                name="ccdocs_att_one_open_holder_per_login",
            ),
            models.CheckConstraint(
                check=Q(dialer_user__isnull=False) | Q(webwork_email__isnull=False),
                name="ccdocs_att_link_has_a_login",
            ),
        ]

    def __str__(self):
        return f"link {self.id}: employee {self.employee_id} -> {self.dialer_user or self.webwork_email}"


class AttendanceNotice(models.Model):
    """A late/out notice filed on the public form (or an excuse from the manager page)."""

    KIND_CHOICES = [("late", "Late"), ("out", "Out")]
    STATUS_CHOICES = [
        ("requested", "Requested"),
        ("excused", "Excused"),
        ("void", "Void"),
    ]

    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="ccdocs_attendance_notices"
    )
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    from_date = models.DateField()
    to_date = models.DateField()
    expected_arrival = models.TimeField(null=True, blank=True)
    reason = models.CharField(max_length=300, blank=True, default="")
    filed_by_self = models.BooleanField(default=True)
    filed_by_name = models.CharField(max_length=120, blank=True, default="")
    filed_at = models.DateTimeField()
    ip_hash = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="requested")
    status_changed_at = models.DateTimeField(null=True, blank=True)
    status_changed_by = models.CharField(max_length=254, blank=True, default="")
    status_note = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-filed_at", "-id"]
        indexes = [models.Index(fields=["from_date", "to_date"])]
        constraints = [
            models.CheckConstraint(
                check=Q(to_date__gte=models.F("from_date")),
                name="ccdocs_att_notice_dates_in_order",
            ),
        ]

    def __str__(self):
        return f"notice {self.id}: employee {self.employee_id} {self.kind} {self.from_date}..{self.to_date}"


class DayResult(models.Model):
    """What the engine decided for one employee on one floor day."""

    STATUS_CHOICES = [
        ("on_time", "On time"),
        ("late", "Late"),
        ("out", "Out"),
        ("excused", "Excused"),
        ("not_scored", "Not scored"),
    ]

    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="ccdocs_attendance_days"
    )
    day = models.DateField()
    status = models.CharField(max_length=12, choices=STATUS_CHOICES)
    scheduled_start = models.TimeField(null=True, blank=True)
    first_login_at = models.DateTimeField(null=True, blank=True)
    minutes_late = models.IntegerField(null=True, blank=True)
    notice = models.ForeignKey(
        AttendanceNotice, null=True, blank=True, on_delete=models.SET_NULL
    )
    closed = models.BooleanField(default=False)
    detail = models.JSONField(default=dict, blank=True)
    run_id = models.CharField(max_length=120, blank=True, default="")
    # Set only by a person on the manager page. The engine never overwrites a
    # day a person excused (same rule as "a voided point is never revived").
    excused_by = models.CharField(max_length=254, blank=True, default="")
    excused_at = models.DateTimeField(null=True, blank=True)
    excuse_reason = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-day", "employee_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "day"], name="ccdocs_att_one_result_per_day"
            ),
        ]

    def __str__(self):
        return f"day {self.day} employee {self.employee_id}: {self.status}"


class PointEntry(models.Model):
    """One attendance point (or two). Void only, never delete."""

    SOURCE_CHOICES = [("engine", "Engine"), ("sarthak", "Sarthak")]

    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="ccdocs_attendance_points"
    )
    day = models.DateField()
    rule_key = models.CharField(max_length=40)
    points = models.DecimalField(max_digits=5, decimal_places=2)
    idem_key = models.CharField(max_length=120, unique=True)
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default="engine")
    day_result = models.ForeignKey(
        DayResult, null=True, blank=True, on_delete=models.SET_NULL
    )
    run_id = models.CharField(max_length=120, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.CharField(max_length=254, blank=True, default="")
    void_reason = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        ordering = ["-day", "-id"]
        indexes = [models.Index(fields=["employee", "day"])]

    @property
    def voided(self):
        return self.voided_at is not None

    def __str__(self):
        return f"point {self.idem_key} = {self.points}{' (void)' if self.voided else ''}"


class Delivery(models.Model):
    """One email or Slack post, claimed once so it is never sent twice."""

    KIND_CHOICES = [
        ("late_email", "Late email"),
        ("out_email", "Out email"),
        ("digest", "Digest email"),
        ("announce", "Announcement"),
        ("slack_notin", "Slack not-in post"),
        ("slack_close", "Slack day summary"),
        ("slack_counts", "Slack counts line"),
        ("slack_hold", "Slack hold post"),
    ]
    STATUS_CHOICES = [
        ("attempting", "Attempting"),
        ("sent", "Sent"),
        ("failed", "Failed"),
    ]

    key = models.CharField(max_length=200, unique=True)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="attempting")
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"delivery {self.key}: {self.status}"
