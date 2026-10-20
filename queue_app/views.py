import json
from datetime import timedelta
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from django.utils import timezone
from django.db.models import Avg, Count, F, Q

from .models import (
    Patient, QueueTicket, ServiceStage, Department, Service, Room, Visit,
    StaffProfile, QueueEvent, RoomAssignmentLog, VisitServiceRequirement
)
from .forms import (
    KioskRegistrationForm, ReceptionRegistrationForm, TriageForm,
    ConsultationCompleteForm, RedirectPatientForm
)
from .services import (
    register_patient_visit, call_next_ticket, recall_ticket, mark_patient_arrived,
    start_consultation, place_on_hold, resume_from_hold, skip_patient,
    complete_consultation, redirect_patient, discharge_visit, update_room_turnover,
    get_estimated_wait_minutes, trigger_auto_assignment_for_stage,
    trigger_auto_assignment_for_room, manual_override_room_assignment,
    release_room_with_turnover
)
from .decorators import role_required, get_or_create_staff_profile


def kiosk_view(request):
    """Self-service kiosk for new or returning patients."""
    if request.method == 'POST':
        form = KioskRegistrationForm(request.POST)
        if form.is_valid():
            name = form.cleaned_data['name'].strip()
            phone_number = form.cleaned_data['phone_number'].strip()
            service = form.cleaned_data['service']
            
            patient, _ = Patient.objects.get_or_create(
                name=name,
                phone_number=phone_number
            )
            visit, stage, created_new = register_patient_visit(
                patient=patient,
                initial_department=service.department,
                initial_service=service,
                user=None
            )
            if not created_new:
                messages.info(request, f"You already have an active ticket ({visit.ticket.ticket_number}) in {service.department.name}.")
            return redirect('print_ticket', ticket_id=visit.ticket.id)
    else:
        form = KioskRegistrationForm()
    
    return render(request, 'kiosk.html', {'form': form})


@login_required
@role_required(['RECEPTION', 'ADMIN'])
def reception_view(request):
    """
    Reception desk dashboard:
    - Search & lookup existing patient records
    - Register walk-ins or scheduled appointments
    - View active queue overview across departments
    - Real-time room assignments and clinician station indicators
    """
    search_query = request.GET.get('q', '').strip()
    selected_dept_id = request.GET.get('department_id')

    # Patient search results
    search_results = []
    if search_query:
        search_results = Patient.objects.filter(
            Q(name__icontains=search_query) |
            Q(phone_number__icontains=search_query) |
            Q(mrn__icontains=search_query)
        )[:10]

    if request.method == 'POST':
        form = ReceptionRegistrationForm(request.POST)
        if form.is_valid():
            existing_id = form.cleaned_data.get('existing_patient_id')
            if existing_id:
                patient = get_object_or_404(Patient, id=existing_id)
                patient.name = form.cleaned_data['name']
                patient.phone_number = form.cleaned_data['phone_number']
                if form.cleaned_data.get('date_of_birth'):
                    patient.date_of_birth = form.cleaned_data['date_of_birth']
                if form.cleaned_data.get('gender'):
                    patient.gender = form.cleaned_data['gender']
                patient.save()
            else:
                patient = form.save()

            service = form.cleaned_data['service']
            is_appointment = form.cleaned_data.get('is_appointment', False)
            priority = int(form.cleaned_data.get('priority', 0))
            priority_reason = form.cleaned_data.get('priority_reason', '')

            visit, stage, created_new = register_patient_visit(
                patient=patient,
                initial_department=service.department,
                initial_service=service,
                is_appointment=is_appointment,
                priority=priority,
                priority_reason=priority_reason,
                user=request.user
            )

            if not created_new:
                messages.warning(request, f"Patient already has an active ticket: {visit.ticket.ticket_number}")
            else:
                msg = f"Ticket {visit.ticket.ticket_number} created successfully for {patient.name}."
                if stage.status == 'ROOM_ASSIGNED' and stage.room:
                    msg += f" (Auto-assigned to {stage.room.name})"
                messages.success(request, msg)
            return redirect('print_ticket', ticket_id=visit.ticket.id)
    else:
        initial_data = {}
        if search_query and search_results:
            first_match = search_results[0]
            initial_data = {
                'existing_patient_id': first_match.id,
                'name': first_match.name,
                'phone_number': first_match.phone_number,
                'mrn': first_match.mrn,
                'date_of_birth': first_match.date_of_birth,
                'gender': first_match.gender
            }
        form = ReceptionRegistrationForm(initial=initial_data)

    # Today's operational visits overview
    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_visits = Visit.objects.filter(registered_at__gte=today_start).select_related('patient', 'ticket').prefetch_related('stages__department', 'stages__service', 'stages__room', 'stages__handled_by').order_by('-registered_at')

    if selected_dept_id:
        today_visits = today_visits.filter(stages__department_id=selected_dept_id).distinct()

    # Metrics
    all_today_stages = ServiceStage.objects.filter(added_to_queue_at__gte=today_start)
    waiting_count = all_today_stages.filter(status='WAITING').count()
    assigned_count = all_today_stages.filter(status='ROOM_ASSIGNED').count()
    called_count = all_today_stages.filter(status__in=['CALLED', 'ARRIVED']).count()
    in_service_count = all_today_stages.filter(status='IN_SERVICE').count()
    completed_count = all_today_stages.filter(status='COMPLETED').count()

    departments = Department.objects.filter(is_active=True)

    context = {
        'form': form,
        'search_query': search_query,
        'search_results': search_results,
        'today_visits': today_visits[:50],
        'waiting_count': waiting_count,
        'assigned_count': assigned_count,
        'called_count': called_count,
        'in_service_count': in_service_count,
        'completed_count': completed_count,
        'departments': departments,
        'selected_dept_id': int(selected_dept_id) if selected_dept_id else None,
    }
    return render(request, 'reception.html', context)


@login_required
def reception_search_api(request):
    """Real-time JSON endpoint for instant patient lookup."""
    q = request.GET.get('q', '').strip()
    if len(q) < 2:
        return JsonResponse({'patients': []})
    
    patients = Patient.objects.filter(
        Q(name__icontains=q) |
        Q(phone_number__icontains=q) |
        Q(mrn__icontains=q)
    )[:10]

    data = [{
        'id': str(p.id),
        'name': p.name,
        'phone_number': p.phone_number or '',
        'mrn': p.mrn,
        'date_of_birth': p.date_of_birth.strftime('%Y-%m-%d') if p.date_of_birth else '',
        'gender': p.gender or ''
    } for p in patients]

    return JsonResponse({'patients': data})


@login_required
@role_required(['TRIAGE', 'CLINICAL', 'ADMIN'])
def triage_view(request):
    """
    Dedicated clinical triage station:
    - Assess patient clinical urgency and record vitals
    - Automatically trigger room assignment when assessment is saved
    - Clear safety protocol instructions for emergency cases
    """
    dept_id = request.GET.get('department_id')
    departments = Department.objects.filter(is_active=True)
    
    stages_qs = ServiceStage.objects.filter(status='WAITING').select_related('visit__patient', 'visit__ticket', 'department', 'service').order_by('-priority', 'added_to_queue_at')

    if dept_id:
        stages_qs = stages_qs.filter(department_id=dept_id)

    if request.method == 'POST':
        stage_id = request.POST.get('stage_id')
        stage = get_object_or_404(ServiceStage, id=stage_id)
        priority = int(request.POST.get('priority', 0))
        priority_reason = request.POST.get('priority_reason', '').strip()
        triage_notes = request.POST.get('triage_notes', '').strip()

        from .services import triage_stage
        triaged_stage = triage_stage(stage, priority, triage_notes, request.user, priority_reason)
        
        if priority == 2:
            messages.warning(request, f"CRITICAL: Ticket {triaged_stage.visit.ticket.ticket_number} escalated to EMERGENCY. Follow hospital emergency response protocol immediately!")
        else:
            msg = f"Triage assessment saved for ticket {triaged_stage.visit.ticket.ticket_number}."
            if triaged_stage.status == 'ROOM_ASSIGNED' and triaged_stage.room:
                msg += f" (Auto-assigned to {triaged_stage.room.name})"
            messages.success(request, msg)

        return redirect(f"{request.path}?{request.GET.urlencode()}")

    context = {
        'waiting_stages': stages_qs,
        'departments': departments,
        'selected_dept_id': int(dept_id) if dept_id else None,
    }
    return render(request, 'triage.html', context)


@login_required
def staff_dashboard_view(request):
    """
    Staff / Doctor Consultation Dashboard:
    - Department & Room binding
    - Next eligible patient in queue
    - Arrived, In-service, Hold, Skip, Transfer, and Complete workflows
    - Real-time room turnover and manual assignment overrides
    """
    profile = get_or_create_staff_profile(request.user)

    dept_id = request.GET.get('department_id')
    if dept_id:
        department = get_object_or_404(Department, id=dept_id)
    elif profile.department:
        department = profile.department
    else:
        department = Department.objects.filter(is_active=True).first()

    if not department:
        return render(request, 'staff_dashboard.html', {'error': 'No active departments configured.'})

    rooms = Room.objects.filter(department=department)
    
    # Active user's room
    user_room = Room.objects.filter(department=department, assigned_staff=request.user).first()
    if not user_room and rooms.filter(status='AVAILABLE').exists():
        user_room = rooms.filter(status='AVAILABLE').first()

    # Active patient being handled by this user OR assigned to user's room
    active_stage = None
    if user_room:
        active_stage = ServiceStage.objects.filter(
            department=department,
            room=user_room,
            status__in=['ROOM_ASSIGNED', 'CALLED', 'ARRIVED', 'IN_SERVICE']
        ).select_related('visit__patient', 'visit__ticket', 'service', 'room').first()

    if not active_stage:
        active_stage = ServiceStage.objects.filter(
            department=department,
            handled_by=request.user,
            status__in=['ROOM_ASSIGNED', 'CALLED', 'ARRIVED', 'IN_SERVICE']
        ).select_related('visit__patient', 'visit__ticket', 'service', 'room').first()

    # Waiting Queue
    waiting_stages = ServiceStage.objects.filter(
        department=department,
        status='WAITING'
    ).select_related('visit__patient', 'visit__ticket', 'service').order_by('-priority', 'added_to_queue_at')

    # Held patients
    held_stages = ServiceStage.objects.filter(
        department=department,
        status='ON_HOLD'
    ).select_related('visit__patient', 'visit__ticket', 'service').order_by('added_to_queue_at')

    # All active room assignments in this department today
    department_assigned_stages = ServiceStage.objects.filter(
        department=department,
        status__in=['ROOM_ASSIGNED', 'CALLED', 'ARRIVED', 'IN_SERVICE']
    ).select_related('visit__patient', 'visit__ticket', 'room', 'handled_by')

    # Completed consultations by current clinician today
    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    completed_today_count = ServiceStage.objects.filter(
        department=department,
        handled_by=request.user,
        status='COMPLETED',
        completed_at__gte=today_start
    ).count()

    # Estimated wait time
    est_wait = get_estimated_wait_minutes(department)

    all_departments = Department.objects.filter(is_active=True)
    all_services = Service.objects.filter(is_active=True)

    context = {
        'profile': profile,
        'department': department,
        'rooms': rooms,
        'user_room': user_room,
        'active_stage': active_stage,
        'waiting_stages': waiting_stages,
        'held_stages': held_stages,
        'department_assigned_stages': department_assigned_stages,
        'completed_today_count': completed_today_count,
        'est_wait': est_wait,
        'all_departments': all_departments,
        'all_services': all_services,
    }
    return render(request, 'staff_dashboard.html', context)


@login_required
@require_POST
def assign_room_view(request):
    """Doctor or clinician assigns themselves to a specific room."""
    room_id = request.POST.get('room_id')
    room = get_object_or_404(Room, id=room_id)
    
    if room.assigned_staff and room.assigned_staff != request.user and room.status == 'OCCUPIED':
        messages.error(request, f"Room {room.name} is currently occupied by {room.assigned_staff.username}.")
    else:
        Room.objects.filter(assigned_staff=request.user).update(assigned_staff=None)
        room.assigned_staff = request.user
        room.save()
        messages.success(request, f"Assigned to {room.name}.")

    return redirect(f"{request.build_absolute_uri('/dashboard/')}?department_id={room.department.id}")


@login_required
@require_POST
def call_next_view(request, department_id):
    """Directly calls the next waiting patient into clinician's room."""
    department = get_object_or_404(Department, id=department_id)
    room_id = request.POST.get('room_id')
    
    if room_id:
        room = get_object_or_404(Room, id=room_id, department=department)
    else:
        room = Room.objects.filter(department=department, assigned_staff=request.user).first()
        if not room:
            room = Room.objects.filter(department=department, status='AVAILABLE').first()

    if not room:
        messages.error(request, "No available consultation room for this call. Please select or release a room.")
        return redirect(f"{request.build_absolute_uri('/dashboard/')}?department_id={department_id}")

    stage = call_next_ticket(department, room, request.user)
    if stage:
        messages.success(request, f"Called ticket {stage.visit.ticket.ticket_number} to {room.name}.")
    else:
        messages.warning(request, "Cannot call next: Queue is empty or room is currently occupied.")

    return redirect(f"{request.build_absolute_uri('/dashboard/')}?department_id={department_id}")


@login_required
@role_required(['CLINICAL', 'RECEPTION', 'ADMIN'])
@require_POST
def manual_room_assign_view(request, stage_id):
    """Authorised staff manual override to assign or reassign patient to a room."""
    stage = get_object_or_404(ServiceStage, id=stage_id)
    room_id = request.POST.get('room_id')
    override_reason = request.POST.get('override_reason', '').strip()
    room = get_object_or_404(Room, id=room_id)

    try:
        manual_override_room_assignment(stage, room, request.user, reason=override_reason)
        messages.success(request, f"Manual override: Ticket {stage.visit.ticket.ticket_number} assigned to {room.name}.")
    except ValueError as e:
        messages.error(request, str(e))

    return redirect(f"{request.build_absolute_uri('/dashboard/')}?department_id={stage.department.id}")


@login_required
@require_POST
def trigger_auto_assign_view(request, department_id):
    """Triggers automated room assignment across all waiting patients and rooms in department."""
    department = get_object_or_404(Department, id=department_id)
    available_rooms = Room.objects.filter(department=department, status='AVAILABLE')
    assigned_count = 0

    for room in available_rooms:
        res = trigger_auto_assignment_for_room(room, user=request.user)
        if res:
            assigned_count += 1

    if assigned_count > 0:
        messages.success(request, f"Automated engine assigned {assigned_count} waiting patient(s) to consultation rooms.")
    else:
        messages.info(request, "No eligible room assignments could be made at this time (all rooms occupied or queue empty).")

    return redirect(f"{request.build_absolute_uri('/dashboard/')}?department_id={department_id}")


@login_required
@require_POST
def mark_arrived_view(request, stage_id):
    """Marks patient as arrived at consultation room."""
    stage = get_object_or_404(ServiceStage, id=stage_id)
    mark_patient_arrived(stage, request.user)
    messages.success(request, f"Patient {stage.visit.ticket.ticket_number} marked arrived.")
    return redirect(f"{request.build_absolute_uri('/dashboard/')}?department_id={stage.department.id}")


@login_required
@require_POST
def start_service_view(request, stage_id):
    """Starts consultation / service timer."""
    stage = get_object_or_404(ServiceStage, id=stage_id)
    start_consultation(stage, request.user)
    messages.success(request, f"Consultation started for ticket {stage.visit.ticket.ticket_number}.")
    return redirect(f"{request.build_absolute_uri('/dashboard/')}?department_id={stage.department.id}")


@login_required
@require_POST
def place_on_hold_view(request, stage_id):
    """Places active patient on hold with recorded reason."""
    stage = get_object_or_404(ServiceStage, id=stage_id)
    reason = request.POST.get('reason', '').strip() or "Awaiting patient return"
    release_room = request.POST.get('release_room') == '1'
    place_on_hold(stage, reason, request.user, release_room=release_room)
    messages.warning(request, f"Ticket {stage.visit.ticket.ticket_number} placed on hold: {reason}")
    return redirect(f"{request.build_absolute_uri('/dashboard/')}?department_id={stage.department.id}")


@login_required
@require_POST
def resume_hold_view(request, stage_id):
    """Resumes held patient back to queue and evaluates auto-assignment."""
    stage = get_object_or_404(ServiceStage, id=stage_id)
    resumed = resume_from_hold(stage, request.user)
    msg = f"Ticket {stage.visit.ticket.ticket_number} returned to queue."
    if resumed.status == 'ROOM_ASSIGNED' and resumed.room:
        msg += f" (Auto-assigned to {resumed.room.name})"
    messages.success(request, msg)
    return redirect(f"{request.build_absolute_uri('/dashboard/')}?department_id={stage.department.id}")


@login_required
@require_POST
def skip_patient_view(request, stage_id):
    """Skips patient with recorded reason and releases room."""
    stage = get_object_or_404(ServiceStage, id=stage_id)
    reason = request.POST.get('reason', '').strip() or "Patient did not appear after multiple calls"
    skip_patient(stage, reason, request.user)
    messages.info(request, f"Ticket {stage.visit.ticket.ticket_number} skipped: {reason}")
    return redirect(f"{request.build_absolute_uri('/dashboard/')}?department_id={stage.department.id}")


@login_required
@require_POST
def recall_ticket_view(request, stage_id):
    """Re-announces ticket on TV screen."""
    stage = get_object_or_404(ServiceStage, id=stage_id)
    recall_ticket(stage, request.user)
    messages.info(request, f"Recalled ticket {stage.visit.ticket.ticket_number} on display.")
    return redirect(f"{request.build_absolute_uri('/dashboard/')}?department_id={stage.department.id}")


@login_required
@require_POST
def complete_consultation_view(request, stage_id):
    """
    Completes current consultation stage:
    - Evaluates next required service (explicit transfer or planned requirements)
    - Releases room turnover state
    - Triggers automated room assignment for freed room
    """
    stage = get_object_or_404(ServiceStage, id=stage_id)
    clinical_notes = request.POST.get('clinical_notes', '').strip()
    room_turnover = request.POST.get('room_turnover', 'AVAILABLE')
    close_visit = request.POST.get('close_visit') == '1'

    next_dept_id = request.POST.get('next_department')
    next_service_id = request.POST.get('next_service')
    transfer_reason = request.POST.get('transfer_reason', '').strip()

    next_dept = None
    next_service = None
    if next_dept_id and next_service_id:
        next_dept = get_object_or_404(Department, id=next_dept_id)
        next_service = get_object_or_404(Service, id=next_service_id)

    completed_stage, next_stage = complete_consultation(
        stage=stage,
        user=request.user,
        clinical_notes=clinical_notes,
        room_turnover=room_turnover,
        close_visit=close_visit,
        next_department=next_dept,
        next_service=next_service,
        transfer_reason=transfer_reason
    )

    msg = f"Consultation for ticket {stage.visit.ticket.ticket_number} completed."
    if next_stage:
        msg += f" Patient routed to {next_stage.department.name} ({next_stage.service.name})."
        if next_stage.status == 'ROOM_ASSIGNED' and next_stage.room:
            msg += f" [Auto-assigned to {next_stage.room.name}]"
    elif close_visit:
        msg += " Patient visit closed and discharged."
    else:
        msg += " Visit marked eligible for discharge."

    messages.success(request, msg)
    return redirect(f"{request.build_absolute_uri('/dashboard/')}?department_id={stage.department.id}")


@login_required
@require_POST
def redirect_patient_view(request, stage_id):
    """Transfers patient to another department and attempts auto-assignment."""
    current_stage = get_object_or_404(ServiceStage, id=stage_id)
    next_dept_id = request.POST.get('next_department')
    next_service_id = request.POST.get('next_service')
    reason = request.POST.get('reason', '').strip()
    clinical_notes = request.POST.get('clinical_notes', '').strip()
    room_turnover = request.POST.get('room_turnover', 'AVAILABLE')

    next_dept = get_object_or_404(Department, id=next_dept_id)
    next_service = get_object_or_404(Service, id=next_service_id)

    new_stage = redirect_patient(
        current_stage=current_stage,
        next_department=next_dept,
        next_service=next_service,
        user=request.user,
        reason=reason,
        clinical_notes=clinical_notes,
        room_turnover=room_turnover
    )
    msg = f"Patient transferred to {next_dept.name} ({next_service.name})."
    if new_stage.status == 'ROOM_ASSIGNED' and new_stage.room:
        msg += f" (Auto-assigned to {new_stage.room.name})"
    messages.success(request, msg)
    return redirect(f"{request.build_absolute_uri('/dashboard/')}?department_id={current_stage.department.id}")


@login_required
@require_POST
def update_room_status_view(request, room_id):
    """Turnover management for rooms. Releases and auto-assigns next waiting patient if AVAILABLE."""
    room = get_object_or_404(Room, id=room_id)
    new_status = request.POST.get('status')
    if new_status in dict(Room.STATUS_CHOICES):
        updated_room, assigned_stage = release_room_with_turnover(
            room,
            new_status,
            request.user,
            release_reason=f"Turnover updated by {request.user.username}"
        )
        msg = f"Room '{updated_room.name}' status updated to {updated_room.get_status_display()}."
        if assigned_stage:
            msg += f" (Auto-assigned waiting Ticket {assigned_stage.visit.ticket.ticket_number})"
        messages.success(request, msg)
    return redirect(f"{request.build_absolute_uri('/dashboard/')}?department_id={room.department.id}")


def print_ticket_view(request, ticket_id):
    """Printable and digital ticket display."""
    ticket = get_object_or_404(QueueTicket, id=ticket_id)
    initial_stage = ticket.visit.stages.first()
    
    context = {
        'ticket': ticket,
        'patient': ticket.visit.patient,
        'department': initial_stage.department.name if initial_stage else "Hospital",
        'service': initial_stage.service.name if initial_stage else "Service",
        'priority': initial_stage.get_priority_display() if initial_stage else "Routine",
        'assigned_room': initial_stage.room.name if (initial_stage and initial_stage.room) else None,
    }
    return render(request, 'ticket.html', context)


def public_display_view(request):
    """Waiting room live TV display."""
    return render(request, 'display.html')


@login_required
@role_required(['ADMIN', 'RECEPTION', 'CLINICAL'])
def report_view(request):
    """Operational analytics & performance metrics."""
    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    days = int(request.GET.get('days', 1))
    start_date = today_start - timedelta(days=days-1)

    stages = ServiceStage.objects.filter(added_to_queue_at__gte=start_date).select_related('department', 'service')
    visits = Visit.objects.filter(registered_at__gte=start_date)

    total_visits = visits.count()
    total_waiting = stages.filter(status='WAITING').count()
    total_in_service = stages.filter(status='IN_SERVICE').count()
    total_completed = stages.filter(status='COMPLETED').count()
    total_skipped = stages.filter(status='SKIPPED').count()
    total_on_hold = stages.filter(status='ON_HOLD').count()

    called_stages = stages.filter(called_at__isnull=False)
    wait_durations = []
    for s in called_stages:
        if s.called_at and s.added_to_queue_at:
            delta_mins = (s.called_at - s.added_to_queue_at).total_seconds() / 60.0
            if delta_mins >= 0:
                wait_durations.append(delta_mins)

    avg_wait = round(sum(wait_durations) / len(wait_durations), 1) if wait_durations else 0.0
    
    if wait_durations:
        sorted_waits = sorted(wait_durations)
        mid = len(sorted_waits) // 2
        median_wait = round((sorted_waits[mid] if len(sorted_waits) % 2 != 0 else (sorted_waits[mid-1] + sorted_waits[mid]) / 2.0), 1)
    else:
        median_wait = 0.0

    consultation_durations = []
    for s in stages.filter(status='COMPLETED', consultation_started_at__isnull=False, completed_at__isnull=False):
        c_mins = (s.completed_at - s.consultation_started_at).total_seconds() / 60.0
        if c_mins >= 0:
            consultation_durations.append(c_mins)

    avg_consultation = round(sum(consultation_durations) / len(consultation_durations), 1) if consultation_durations else 0.0

    dept_stats = []
    for dept in Department.objects.filter(is_active=True):
        dept_stages = stages.filter(department=dept)
        dept_wait_list = []
        for s in dept_stages.filter(called_at__isnull=False):
            d_mins = (s.called_at - s.added_to_queue_at).total_seconds() / 60.0
            if d_mins >= 0:
                dept_wait_list.append(d_mins)
        dept_avg_wait = round(sum(dept_wait_list) / len(dept_wait_list), 1) if dept_wait_list else 0.0

        dept_stats.append({
            'department': dept.name,
            'waiting': dept_stages.filter(status='WAITING').count(),
            'in_service': dept_stages.filter(status='IN_SERVICE').count(),
            'completed': dept_stages.filter(status='COMPLETED').count(),
            'skipped': dept_stages.filter(status='SKIPPED').count(),
            'avg_wait': dept_avg_wait,
            'total': dept_stages.count()
        })

    rooms = Room.objects.all().select_related('department', 'assigned_staff')

    context = {
        'days': days,
        'total_visits': total_visits,
        'total_waiting': total_waiting,
        'total_in_service': total_in_service,
        'total_completed': total_completed,
        'total_skipped': total_skipped,
        'total_on_hold': total_on_hold,
        'avg_wait': avg_wait,
        'median_wait': median_wait,
        'avg_consultation': avg_consultation,
        'dept_stats': dept_stats,
        'rooms': rooms,
    }
    return render(request, 'report.html', context)
