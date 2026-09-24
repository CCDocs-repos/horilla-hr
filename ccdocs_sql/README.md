# CCDocs SQL

`ccdocs_payout_chase.sql` creates the table behind the "Chase for details"
button on the employee profile (`employee/views.py`, the `payout_chase_*` views).

- **Production:** the table and the `Payroll Chasers` group already exist. Nothing to do.
- **Brand-new database:** run `ccdocs_payout_chase.sql` once (it is safe to re-run),
  then create a group named exactly `Payroll Chasers` in Django admin
  (`/admin/auth/group/`) and add the people who may use the button.
  Superusers can always use it.

The list of details a person can be chased for is `ccdocs_payout_fields.json`
at the repo root (the view reads it from `/app/ccdocs_payout_fields.json`).
