import json
import logging
import traceback
from datetime import timedelta

from odoo import _, api, fields, models

from .channel_error import ChannelError

_logger = logging.getLogger(__name__)

JOB_TYPES = [
    ("import_order", "Import order"),
    ("import_product", "Import product"),
    ("push_stock", "Push stock"),
    ("push_fulfillment", "Push shipment"),
    ("other", "Other"),
]


class ChannelJob(models.Model):
    """One visible, retryable unit of work between Odoo and a channel."""

    _name = "channel.job"
    _description = "Sales Channel Job"
    _order = "id desc"

    name = fields.Char(required=True)
    store_id = fields.Many2one(
        "channel.store", required=True, ondelete="cascade", index=True
    )
    channel_type = fields.Selection(related="store_id.channel_type")
    job_type = fields.Selection(JOB_TYPES, required=True, default="other", index=True)
    state = fields.Selection(
        [
            ("pending", "Waiting"),
            ("done", "Done"),
            ("failed", "Needs attention"),
        ],
        default="pending",
        required=True,
        index=True,
    )
    payload = fields.Text(help="JSON arguments of the job.")
    dedupe_key = fields.Char(index=True)
    external_ref = fields.Char(string="Channel Reference")
    error = fields.Text(string="What went wrong")
    traceback = fields.Text(groups="channel_connector_base.group_channel_manager")
    retry_count = fields.Integer(copy=False)
    max_retries = fields.Integer(default=5)
    next_try = fields.Datetime(default=fields.Datetime.now, index=True)
    date_done = fields.Datetime(copy=False)
    res_model = fields.Char(copy=False)
    res_id = fields.Many2oneReference(model_field="res_model", copy=False)

    # -- helpers -------------------------------------------------------------

    def get_payload(self):
        self.ensure_one()
        return json.loads(self.payload or "{}")

    @api.model
    def _enqueue(self, store, job_type, name, payload=None, dedupe_key=None, external_ref=None):
        """Create a job unless an identical one is already waiting or done."""
        if dedupe_key:
            existing = self.search(
                [
                    ("store_id", "=", store.id),
                    ("dedupe_key", "=", dedupe_key),
                    ("state", "in", ("pending", "done")),
                ],
                limit=1,
            )
            if existing:
                return self.browse()
        return self.create(
            {
                "name": name,
                "store_id": store.id,
                "job_type": job_type,
                "payload": json.dumps(payload or {}, default=str),
                "dedupe_key": dedupe_key,
                "external_ref": external_ref,
            }
        )

    # -- execution -----------------------------------------------------------

    @api.model
    def _run_due_jobs(self, limit=100):
        jobs = self.search(
            [
                ("state", "=", "pending"),
                ("next_try", "<=", fields.Datetime.now()),
                ("store_id.state", "=", "connected"),
            ],
            order="id",
            limit=limit,
        )
        jobs._run()
        return len(jobs)

    def _run(self):
        for job in self:
            if job.state != "pending":
                continue
            try:
                with self.env.cr.savepoint():
                    result = job.store_id._run_job(job)
            except Exception as exc:  # noqa: BLE001 - recorded on the job
                job._register_failure(exc)
            else:
                values = {
                    "state": "done",
                    "error": False,
                    "traceback": False,
                    "date_done": fields.Datetime.now(),
                }
                if result is not None and getattr(result, "_name", None):
                    values.update(res_model=result._name, res_id=result.id)
                job.write(values)

    def _register_failure(self, exc):
        self.ensure_one()
        if isinstance(exc, ChannelError):
            message, tb = exc.args[0] if exc.args else str(exc), False
        else:
            _logger.warning("Channel job %s failed", self.id, exc_info=True)
            message = _("Unexpected error: %s", exc)
            tb = traceback.format_exc()
        retries = self.retry_count + 1
        exhausted = retries >= self.max_retries
        self.write(
            {
                "error": message,
                "traceback": tb,
                "retry_count": retries,
                "state": "failed" if exhausted else "pending",
                # 2, 4, 8... minutes between attempts
                "next_try": fields.Datetime.now() + timedelta(minutes=2**retries),
            }
        )
        if exhausted:
            self.store_id.message_post(
                body=_("Job '%(job)s' failed %(n)s times: %(error)s", job=self.name, n=retries, error=message)
            )

    # -- buttons -------------------------------------------------------------

    def action_retry(self):
        """Run the job again right now."""
        self.filtered(lambda j: j.state != "done").write(
            {"state": "pending", "retry_count": 0, "next_try": fields.Datetime.now()}
        )
        self._run()
        return True

    def action_mark_done(self):
        self.write({"state": "done", "date_done": fields.Datetime.now()})

    def action_open_result(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": self.res_model,
            "res_id": self.res_id,
            "view_mode": "form",
        }

    @api.autovacuum
    def _gc_old_jobs(self):
        limit = fields.Datetime.now() - timedelta(days=60)
        self.search([("state", "=", "done"), ("date_done", "<", limit)]).unlink()
