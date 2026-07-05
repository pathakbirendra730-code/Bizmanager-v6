"""
notification/queue.py — The seam where async job processing plugs in.

Today, `enqueue()` just calls the function immediately (synchronous) —
sending a couple of emails/SMS per signup doesn't need a real queue yet,
and this app has no Celery/Redis infrastructure to depend on.

When that changes (e.g. bulk invoice emails, marketing blasts), this is
the ONLY file that needs to change. Every call site in this codebase
goes through `enqueue()`, never calls a provider directly — so swapping
this for `celery_app.send_task(...)` or `rq_queue.enqueue(...)` doesn't
require touching manager.py, email_service.py, sms_service.py, or
anything that calls them.

Example future implementation:

    from celery import shared_task

    @shared_task(max_retries=3)
    def _run(fn_path, args, kwargs):
        ...

    def enqueue(fn, *args, **kwargs):
        _run.delay(f"{fn.__module__}.{fn.__name__}", args, kwargs)
"""


def enqueue(fn, *args, **kwargs):
    """Run `fn(*args, **kwargs)` — synchronously for now. Returns
    whatever `fn` returns, so callers don't need to change when this
    becomes properly async (a real queue wouldn't return the result
    inline, so callers that depend on the return value would need to
    change then — but nothing calling this today blocks on that)."""
    return fn(*args, **kwargs)
