# Import views — request handling, response rendering, form handling only.
from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.scm.decorators import scm_login_required

from .forms import ImportUploadForm
from .models import ImportJob
from .selectors import get_import_errors, get_import_rows, get_job_summary, get_team_import_jobs
from .services import confirm_import_job, create_import_job, parse_import_job, validate_import_job


@scm_login_required
def import_list(request):
    team = request.default_team
    jobs = get_team_import_jobs(team)
    return render(request, "scm/imports/pages/import_list.html", {"jobs": jobs})


@scm_login_required
def import_upload(request):
    team = request.default_team
    if request.method == "POST":
        form = ImportUploadForm(request.POST, request.FILES)
        if form.is_valid():
            job = create_import_job(
                team=team,
                created_by=request.user,
                file=form.cleaned_data["file"],
                import_type=form.cleaned_data["import_type"],
            )
            messages.success(request, _("File uploaded. Click 'Parse' to continue."))
            return redirect("imports:detail", pk=job.pk)
    else:
        form = ImportUploadForm()
    return render(request, "scm/imports/pages/import_upload.html", {"form": form})


@scm_login_required
def import_detail(request, pk):
    team = request.default_team
    from .selectors import get_import_job

    job = get_import_job(team, pk)
    rows = get_import_rows(job)
    errors = get_import_errors(job)
    summary = get_job_summary(job)
    context = {
        "job": job,
        "rows": rows,
        "errors": errors,
        "summary": summary,
        "can_parse": job.status == ImportJob.Status.UPLOADED,
        "can_validate": job.status == ImportJob.Status.PARSED,
        "can_confirm": job.status == ImportJob.Status.VALIDATED,
    }
    return render(request, "scm/imports/pages/import_detail.html", context)


@scm_login_required
@require_POST
def import_parse(request, pk):
    team = request.default_team
    from .selectors import get_import_job

    job = get_import_job(team, pk)
    if job.status != ImportJob.Status.UPLOADED:
        messages.error(request, _("This import has already been parsed."))
        return redirect("imports:detail", pk=pk)
    try:
        parse_import_job(job)
        messages.success(request, _("File parsed. Click 'Validate' to check for errors."))
    except Exception:
        messages.error(request, _("Parsing failed. Please check the file and try again."))
    return redirect("imports:detail", pk=pk)


@scm_login_required
@require_POST
def import_validate(request, pk):
    team = request.default_team
    from .selectors import get_import_job

    job = get_import_job(team, pk)
    if job.status != ImportJob.Status.PARSED:
        messages.error(request, _("Import must be parsed before validation."))
        return redirect("imports:detail", pk=pk)
    try:
        validate_import_job(job)
        messages.success(request, _("Validation complete. Review the results below."))
    except Exception:
        messages.error(request, _("Validation failed unexpectedly."))
    return redirect("imports:detail", pk=pk)


@scm_login_required
@require_POST
def import_confirm(request, pk):
    team = request.default_team
    from .selectors import get_import_job

    job = get_import_job(team, pk)
    if job.status != ImportJob.Status.VALIDATED:
        messages.error(request, _("Import must be validated before confirming."))
        return redirect("imports:detail", pk=pk)
    try:
        update_existing = request.POST.get("update_existing") == "1"
        confirm_import_job(job, update_existing=update_existing)
        messages.success(
            request,
            _("Import completed: %(processed)s rows processed.") % {"processed": job.processed_rows},
        )
    except Exception:
        messages.error(request, _("Import failed unexpectedly."))
    return redirect("imports:detail", pk=pk)
