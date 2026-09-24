"""
The public late/out notice form. Plain words; every rule is checked here.
"""

from datetime import timedelta

from django import forms

MAX_DAYS_AHEAD = 14  # first day can be today .. today + 14
MAX_SPAN_DAYS = 14  # last day can be at most 13 days after the first day


class NoticeForm(forms.Form):
    employee = forms.TypedChoiceField(
        coerce=int,
        label="Who is this for?",
        error_messages={
            "required": "Pick a name.",
            "invalid_choice": "Pick a name from the list.",
        },
    )
    kind = forms.ChoiceField(
        label="What is happening?",
        choices=[
            ("late", "I will be late"),
            ("out", "I will be out (not coming in)"),
        ],
        widget=forms.RadioSelect,
        error_messages={
            "required": "Pick late or out.",
            "invalid_choice": "Pick late or out.",
        },
    )
    from_date = forms.DateField(
        label="Which day?",
        widget=forms.DateInput(attrs={"type": "date"}),
        error_messages={"required": "Pick a day.", "invalid": "Enter a real date."},
    )
    to_date = forms.DateField(
        label="Last day (only if you will be out more than one day)",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        error_messages={"invalid": "Enter a real date."},
    )
    expected_arrival = forms.TimeField(
        label="What time will you get here? (Eastern time, only if late)",
        required=False,
        widget=forms.TimeInput(attrs={"type": "time"}),
        error_messages={"invalid": "Enter a real time, like 12:30."},
    )
    reason = forms.CharField(
        label="Short reason",
        max_length=300,
        widget=forms.Textarea(attrs={"rows": 3, "maxlength": 300}),
        error_messages={
            "required": "Write a short reason.",
            "max_length": "Keep the reason under 300 characters.",
        },
    )
    filing_for_someone_else = forms.BooleanField(
        label="I am filling this in for someone else", required=False
    )
    filed_by_name = forms.CharField(label="Your name", max_length=120, required=False)
    # Honeypot: hidden from people; a bot that fills it gets a fake "thanks".
    website = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "off", "tabindex": "-1"}),
    )

    def __init__(self, *args, employee_choices, today, **kwargs):
        super().__init__(*args, **kwargs)
        self.today = today
        self.fields["employee"].choices = [("", "Pick a name")] + list(employee_choices)
        last = today + timedelta(days=MAX_DAYS_AHEAD)
        self.fields["from_date"].widget.attrs.update(
            {"min": today.isoformat(), "max": last.isoformat()}
        )
        self.fields["to_date"].widget.attrs.update({"min": today.isoformat()})

    def is_bot(self):
        return bool((self.data.get("website") or "").strip())

    def clean_reason(self):
        return (self.cleaned_data.get("reason") or "").strip()

    def clean_filed_by_name(self):
        return (self.cleaned_data.get("filed_by_name") or "").strip()

    def clean(self):
        cleaned = super().clean()
        from_date = cleaned.get("from_date")
        to_date = cleaned.get("to_date")
        if from_date:
            if from_date < self.today:
                self.add_error("from_date", "Pick today or a day after today.")
            elif from_date > self.today + timedelta(days=MAX_DAYS_AHEAD):
                self.add_error(
                    "from_date", f"Pick a day in the next {MAX_DAYS_AHEAD} days."
                )
            if to_date is None:
                cleaned["to_date"] = from_date
            elif to_date < from_date:
                self.add_error(
                    "to_date", "The last day cannot be before the first day."
                )
            elif to_date > from_date + timedelta(days=MAX_SPAN_DAYS - 1):
                self.add_error(
                    "to_date", f"One form can cover at most {MAX_SPAN_DAYS} days."
                )
        if cleaned.get("kind") == "late" and not cleaned.get("expected_arrival"):
            self.add_error("expected_arrival", "Tell us what time you will get here.")
        if cleaned.get("kind") == "out":
            cleaned["expected_arrival"] = None
        if not cleaned.get("reason") and "reason" not in self.errors:
            self.add_error("reason", "Write a short reason.")
        if cleaned.get("filing_for_someone_else"):
            if len(cleaned.get("filed_by_name") or "") < 2:
                self.add_error("filed_by_name", "Write your own name.")
        else:
            cleaned["filed_by_name"] = ""
        return cleaned
