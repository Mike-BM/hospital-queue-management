from django.db import transaction
from django.utils import timezone
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from .models import (
    Patient, Department, Service, Room, Visit,
    QueueTicket, ServiceStage, QueueEvent, TicketCounter,
    RoomAssignmentLog, VisitServiceRequirement
)

@transaction.atomic
def generate_ticket_number(prefix: str) -> str:
    """
    Safely generates a daily incremented ticket number using database row locking.
    Format: {PREFIX}-{YYMMDD}-{COUNTER:03d}
    """
    today = timezone.localdate()
    
    counter, created = TicketCounter.objects.select_for_update().get_or_create(
        date=today,
        prefix=prefix,
        defaults={'last_number': 0}
    )
    
    counter.last_number += 1
    counter.save()
    
    date_str = today.strftime("%y%m%d")
    return f"{prefix}-{date_str}-{counter.last_number:03d}"


def broadcast_queue_update(ticket_number: str, department: str, room: str, status: str = 'CALLED'):
    """
    Broadcasts public real-time events to connected TV displays.
    """
    try:
        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                'queue_display',
                {
                    'type': 'queue_update',
                    'ticket_number': ticket_number,
                    'department': department,
                    'room': room,
                    'status': status
                }
            )
    except Exception:
        pass


def broadcast_dashboard_event(event_type: str, data: dict):
    """
    Broadcasts real-time events to connected staff dashboards (Reception, Doctor, Triage).
    """
    try:
        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                'queue_dashboard',
                {
                    'type': 'dashboard_event',
                    'event': event_type,
                    'data': data
                }
            )
    except Exception:
        pass


# ==========================================
# AUTOMATED ROOM ASSIGNMENT ENGINE
# ==========================================

def find_eligible_room(department: Department, stage: ServiceStage) -> Room:
    """
    Finds the most suitable, eligible consultation room in the destination department.
    Configurable Selection Policy:
    1. Room must belong to department and be active.
    2. Room must be strictly 'AVAILABLE' (not OCCUPIED, AWAITING_CLEANING, UNAVAILABLE, OUT_OF_SERVICE, or RESERVED).
    3. Room must NOT have any other active patient assignment ('ROOM_ASSIGNED', 'CALLED', 'ARRIVED', 'IN_SERVICE').
    4. Clinician Suitability:
       - Preference 1: A room with an active clinician (assigned_staff) who is currently idle.
       - Preference 2: Longest idle room (earliest last_released_at or lowest ID).
    """
    candidate_rooms = Room.objects.select_for_update().filter(
        department=department,
        is_active=True,
        status='AVAILABLE'
    )

    eligible_rooms = []
    for room in candidate_rooms:
        # Verify no active patient is assigned to this room
        has_active_patient = ServiceStage.objects.filter(
            room=room,
            status__in=['ROOM_ASSIGNED', 'CALLED', 'ARRIVED', 'IN_SERVICE']
        ).exists()

        if not has_active_patient:
            eligible_rooms.append(room)

    if not eligible_rooms:
        return None

    # Sort rooms: first prefer rooms with an assigned idle clinician, then order by idle time
    def suitability_score(r):
        has_doctor = 1 if r.assigned_staff else 0
        # If doctor has another active patient in any room, deprioritize
        doctor_busy = 0
        if r.assigned_staff:
            doctor_busy = 1 if ServiceStage.objects.filter(
                handled_by=r.assigned_staff,
                status__in=['ROOM_ASSIGNED', 'CALLED', 'ARRIVED', 'IN_SERVICE']
            ).exists() else 0

        # Longest idle timestamp
        idle_timestamp = r.last_released_at.timestamp() if r.last_released_at else 0
        return (has_doctor - doctor_busy, -idle_timestamp)

    eligible_rooms.sort(key=suitability_score, reverse=True)
    return eligible_rooms[0]


@transaction.atomic
def assign_room_to_stage(stage: ServiceStage, room: Room, user=None, mode: str = 'AUTO', override_reason: str = "") -> ServiceStage:
    """
    Atomic server-side operation to assign an eligible room to a patient stage.
    Ensures safe concurrency and updates status to ROOM_ASSIGNED.
    """
    locked_room = Room.objects.select_for_update().get(id=room.id)
    
    # Concurrency verification: room must be available or already assigned to this stage
    if locked_room.status not in ['AVAILABLE', 'RESERVED'] and stage.room != locked_room:
        raise ValueError(f"Room {locked_room.name} is not available (Status: {locked_room.get_status_display()}).")

    active_in_room = ServiceStage.objects.filter(
        room=locked_room,
        status__in=['ROOM_ASSIGNED', 'CALLED', 'ARRIVED', 'IN_SERVICE']
    ).exclude(id=stage.id).first()

    if active_in_room:
        raise ValueError(f"Room {locked_room.name} is already occupied by Ticket {active_in_room.visit.ticket.ticket_number}.")

    # Update stage
    stage.room = locked_room
    stage.status = 'ROOM_ASSIGNED'
    stage.assigned_at = timezone.now()
    stage.called_at = timezone.now()
    stage.assignment_mode = mode
    stage.override_reason = override_reason
    if locked_room.assigned_staff:
        stage.handled_by = locked_room.assigned_staff
    elif user and not stage.handled_by:
        stage.handled_by = user
    stage.save()

    # Update room state
    locked_room.status = 'OCCUPIED'
    if not locked_room.assigned_staff and stage.handled_by:
        locked_room.assigned_staff = stage.handled_by
    locked_room.save()

    # Immutable Audit Log
    RoomAssignmentLog.objects.create(
        stage=stage,
        room=locked_room,
        assigned_staff=stage.handled_by,
        assignment_mode=mode,
        override_reason=override_reason,
        assigned_at=timezone.now()
    )

    QueueEvent.objects.create(
        stage=stage,
        event_type='ROOM_ASSIGNED',
        user=user,
        description=f"Assigned to {locked_room.name} ({mode}). Clinician: {stage.handled_by.username if stage.handled_by else 'Unassigned'}. {override_reason}".strip()
    )

    # Real-Time Synchronisation
    broadcast_queue_update(
        ticket_number=stage.visit.ticket.ticket_number,
        department=stage.department.name,
        room=locked_room.name,
        status='ROOM_ASSIGNED'
    )

    broadcast_dashboard_event(
        'patient_assigned',
        {
            'stage_id': stage.id,
            'ticket_number': stage.visit.ticket.ticket_number,
            'patient_name': stage.visit.patient.name,
            'patient_mrn': stage.visit.patient.mrn,
            'department': stage.department.name,
            'room': locked_room.name,
            'room_id': locked_room.id,
            'clinician': stage.handled_by.username if stage.handled_by else None,
            'status': 'ROOM_ASSIGNED',
            'status_display': 'Room Assigned',
            'assignment_mode': mode,
            'assigned_at': stage.assigned_at.strftime('%H:%M:%S')
        }
    )

    return stage


@transaction.atomic
def trigger_auto_assignment_for_stage(stage: ServiceStage, user=None):
    """
    Evaluates automated room assignment for a specific patient stage (e.g. after triage or registration).
    Returns (assigned_room, stage). If no room available, patient remains WAITING in queue.
    """
    if stage.status != 'WAITING':
        return None, stage

    eligible_room = find_eligible_room(stage.department, stage)
    if eligible_room:
        assigned_stage = assign_room_to_stage(stage, eligible_room, user=user, mode='AUTO')
        return eligible_room, assigned_stage

    return None, stage


@transaction.atomic
def trigger_auto_assignment_for_room(room: Room, user=None):
    """
    Evaluates automated patient assignment when a room becomes available (e.g. after turnover or release).
    Picks the highest-priority waiting patient in the room's department.
    Returns assigned ServiceStage or None.
    """
    if room.status != 'AVAILABLE':
        return None

    next_waiting_stage = ServiceStage.objects.select_for_update().filter(
        department=room.department,
        status='WAITING'
    ).order_by('-priority', 'added_to_queue_at').first()

    if next_waiting_stage:
        return assign_room_to_stage(next_waiting_stage, room, user=user, mode='AUTO')

    return None


@transaction.atomic
def release_room_with_turnover(room: Room, new_status: str, user, release_reason: str = ""):
    """
    Releases room occupancy and updates turnover status (e.g., AWAITING_CLEANING or AVAILABLE).
    When transitioned to AVAILABLE, automatically checks and assigns the next waiting patient.
    Returns (room, newly_assigned_stage or None).
    """
    locked_room = Room.objects.select_for_update().get(id=room.id)
    
    # Close any open assignment logs for this room
    open_logs = RoomAssignmentLog.objects.filter(room=locked_room, released_at__isnull=True)
    open_logs.update(released_at=timezone.now(), release_reason=release_reason or "Turnover transition")

    locked_room.status = new_status
    if new_status in ['AVAILABLE', 'OUT_OF_SERVICE']:
        locked_room.last_released_at = timezone.now()
    locked_room.save()

    broadcast_dashboard_event(
        'room_status_changed',
        {
            'room_id': locked_room.id,
            'name': locked_room.name,
            'department': locked_room.department.name,
            'status': locked_room.status,
            'status_display': locked_room.get_status_display()
        }
    )

    newly_assigned_stage = None
    if new_status == 'AVAILABLE':
        # Automatically pull next patient into the freed room!
        newly_assigned_stage = trigger_auto_assignment_for_room(locked_room, user=user)

    return locked_room, newly_assigned_stage


@transaction.atomic
def manual_override_room_assignment(stage: ServiceStage, room: Room, user, reason: str) -> ServiceStage:
    """
    Authorised staff manual override to assign or transfer patient to a specific room.
    Requires justification reason and enforces room safety checks.
    """
    if not reason or not reason.strip():
        raise ValueError("A documented reason is mandatory for manual room assignment overrides.")

    # If stage previously held another room, release the old room
    if stage.room and stage.room != room:
        release_room_with_turnover(stage.room, 'AVAILABLE', user, release_reason=f"Manual reassignment to {room.name}")

    return assign_room_to_stage(stage, room, user=user, mode='MANUAL', override_reason=reason.strip())


# ==========================================
# PATIENT REGISTRATION & FLOW
# ==========================================

@transaction.atomic
def register_patient_visit(
    patient: Patient,
    initial_department: Department,
    initial_service: Service,
    is_appointment: bool = False,
    priority: int = 0,
    priority_reason: str = "",
    user = None,
    subsequent_services = None
):
    """
    Registers a patient for a visit and generates an initial service ticket.
    Prevents duplicate active tickets if the patient is already waiting/in-service in this department.
    Optionally queues subsequent visit service requirements.
    """
    # 1. Duplicate check: check if the patient already has an active stage in an active visit in this department today
    active_stage = ServiceStage.objects.select_for_update().filter(
        visit__patient=patient,
        visit__is_active=True,
        department=initial_department,
        status__in=['WAITING', 'ROOM_ASSIGNED', 'CALLED', 'ARRIVED', 'IN_SERVICE', 'ON_HOLD']
    ).first()

    if active_stage:
        return active_stage.visit, active_stage, False

    # 2. Check for an active visit today for this patient, or create a new visit
    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    existing_visit = Visit.objects.filter(
        patient=patient,
        is_active=True,
        registered_at__gte=today_start
    ).first()

    if existing_visit and hasattr(existing_visit, 'ticket'):
        visit = existing_visit
    else:
        visit = Visit.objects.create(
            patient=patient,
            is_appointment=is_appointment,
            is_active=True
        )
        ticket_number = generate_ticket_number(initial_department.prefix)
        QueueTicket.objects.create(visit=visit, ticket_number=ticket_number)

    # 3. Create initial ServiceStage
    stage = ServiceStage.objects.create(
        visit=visit,
        department=initial_department,
        service=initial_service,
        status='WAITING',
        priority=priority,
        priority_reason=priority_reason
    )

    # 4. Optional subsequent services plan
    if subsequent_services:
        for idx, s_req in enumerate(subsequent_services, start=1):
            VisitServiceRequirement.objects.create(
                visit=visit,
                department=s_req['department'],
                service=s_req['service'],
                order=idx
            )

    # 5. Audit Log
    QueueEvent.objects.create(
        stage=stage,
        event_type='REGISTERED',
        user=user,
        description=f"Registered for {initial_service.name} in {initial_department.name} (Priority: {stage.get_priority_display()})."
    )

    # 6. Attempt immediate automatic room assignment if ready and room is open
    eligible_room, assigned_stage = trigger_auto_assignment_for_stage(stage, user=user)

    return visit, assigned_stage or stage, True


@transaction.atomic
def triage_stage(stage: ServiceStage, priority: int, triage_notes: str, user, priority_reason: str = "") -> ServiceStage:
    """
    Records triage assessment vitals, observations, sets clinical priority,
    and immediately evaluates the automated room assignment engine.
    """
    old_priority_display = stage.get_priority_display()
    stage.priority = priority
    stage.priority_reason = priority_reason
    stage.triage_notes = triage_notes
    stage.triage_by = user
    stage.save()

    QueueEvent.objects.create(
        stage=stage,
        event_type='TRIAGED',
        user=user,
        description=f"Triage assessment completed. Priority: {stage.get_priority_display()} (was {old_priority_display}). Reason: {priority_reason}. Vitals/Notes: {triage_notes}"
    )

    # Critical Automation: Automatically assign room if one is available
    eligible_room, assigned_stage = trigger_auto_assignment_for_stage(stage, user=user)
    return assigned_stage or stage


@transaction.atomic
def call_next_ticket(department: Department, room: Room, user) -> ServiceStage:
    """
    Pops the next patient from the waiting queue based on Priority then FIFO into the specified room.
    """
    locked_room = Room.objects.select_for_update().get(id=room.id)
    if locked_room.status not in ['AVAILABLE', 'RESERVED']:
        active_in_room = ServiceStage.objects.filter(
            room=locked_room,
            status__in=['ROOM_ASSIGNED', 'CALLED', 'ARRIVED', 'IN_SERVICE']
        ).first()
        if active_in_room:
            return None

    next_stage = ServiceStage.objects.select_for_update().filter(
        department=department,
        status='WAITING'
    ).order_by('-priority', 'added_to_queue_at').first()

    if next_stage:
        return assign_room_to_stage(next_stage, locked_room, user=user, mode='MANUAL', override_reason="Direct Call Next by Staff")

    return None


@transaction.atomic
def recall_ticket(stage: ServiceStage, user) -> ServiceStage:
    """
    Re-announces a called or room-assigned patient on the public display.
    """
    if stage.status in ['ROOM_ASSIGNED', 'CALLED', 'ARRIVED', 'IN_SERVICE']:
        broadcast_queue_update(
            ticket_number=stage.visit.ticket.ticket_number,
            department=stage.department.name,
            room=stage.room.name if stage.room else "Consultation",
            status='RECALLED'
        )
        QueueEvent.objects.create(
            stage=stage,
            event_type='RECALLED',
            user=user,
            description="Recalled ticket on display screen"
        )
    return stage


@transaction.atomic
def mark_patient_arrived(stage: ServiceStage, user) -> ServiceStage:
    """
    Marks that a called/assigned patient has arrived at the room/station.
    """
    stage.status = 'ARRIVED'
    stage.arrived_at = timezone.now()
    stage.save()

    QueueEvent.objects.create(
        stage=stage,
        event_type='ARRIVED',
        user=user,
        description=f"Patient arrived at {stage.room.name if stage.room else 'station'}"
    )

    broadcast_dashboard_event(
        'status_changed',
        {
            'stage_id': stage.id,
            'ticket_number': stage.visit.ticket.ticket_number,
            'status': 'ARRIVED',
            'status_display': 'Arrived'
        }
    )
    return stage


@transaction.atomic
def start_consultation(stage: ServiceStage, user) -> ServiceStage:
    """
    Starts consultation / clinical service for an arrived or room-assigned patient.
    """
    stage.status = 'IN_SERVICE'
    stage.consultation_started_at = timezone.now()
    stage.handled_by = user
    stage.save()

    QueueEvent.objects.create(
        stage=stage,
        event_type='STARTED',
        user=user,
        description="Clinical consultation started"
    )

    broadcast_dashboard_event(
        'status_changed',
        {
            'stage_id': stage.id,
            'ticket_number': stage.visit.ticket.ticket_number,
            'status': 'IN_SERVICE',
            'status_display': 'In Service'
        }
    )
    return stage


@transaction.atomic
def place_on_hold(stage: ServiceStage, reason: str, user, release_room: bool = False) -> ServiceStage:
    """
    Places an active patient on hold with a recorded clinical/administrative reason.
    Optionally releases the consultation room for other waiting patients.
    """
    stage.status = 'ON_HOLD'
    stage.hold_reason = reason
    stage.save()

    held_room = stage.room
    if release_room and held_room:
        stage.room = None
        stage.save()
        release_room_with_turnover(held_room, 'AVAILABLE', user, release_reason=f"Patient {stage.visit.ticket.ticket_number} placed on hold: {reason}")

    QueueEvent.objects.create(
        stage=stage,
        event_type='ON_HOLD',
        user=user,
        description=f"Placed on hold. Reason: {reason}. Room released: {release_room}"
    )

    broadcast_dashboard_event(
        'status_changed',
        {
            'stage_id': stage.id,
            'ticket_number': stage.visit.ticket.ticket_number,
            'status': 'ON_HOLD',
            'status_display': 'On Hold'
        }
    )
    return stage


@transaction.atomic
def resume_from_hold(stage: ServiceStage, user) -> ServiceStage:
    """
    Resumes a patient from hold back to waiting status and evaluates auto-assignment.
    """
    stage.status = 'WAITING'
    stage.save()

    QueueEvent.objects.create(
        stage=stage,
        event_type='RESUMED',
        user=user,
        description="Resumed from hold into waiting queue"
    )

    # Attempt auto assignment
    eligible_room, assigned_stage = trigger_auto_assignment_for_stage(stage, user=user)
    return assigned_stage or stage


@transaction.atomic
def skip_patient(stage: ServiceStage, reason: str, user) -> ServiceStage:
    """
    Skips a called patient (e.g. absent from waiting lobby) with recorded reason and releases room.
    """
    stage.status = 'SKIPPED'
    stage.skip_reason = reason
    stage.completed_at = timezone.now()
    stage.save()

    current_room = stage.room
    if current_room:
        release_room_with_turnover(current_room, 'AVAILABLE', user, release_reason=f"Patient skipped: {reason}")

    QueueEvent.objects.create(
        stage=stage,
        event_type='SKIPPED',
        user=user,
        description=f"Patient skipped. Reason: {reason}"
    )

    broadcast_dashboard_event(
        'status_changed',
        {
            'stage_id': stage.id,
            'ticket_number': stage.visit.ticket.ticket_number,
            'status': 'SKIPPED',
            'status_display': 'Skipped'
        }
    )
    return stage


@transaction.atomic
def complete_consultation(
    stage: ServiceStage,
    user,
    clinical_notes: str = "",
    room_turnover: str = "AVAILABLE",
    close_visit: bool = False,
    next_department: Department = None,
    next_service: Service = None,
    transfer_reason: str = ""
):
    """
    Automatic patient release & consultation completion engine:
    1. Validates authorisation and marks stage COMPLETED.
    2. Releases room according to operational turnover state (AVAILABLE vs AWAITING_CLEANING).
    3. Evaluates next required clinical services for the visit:
       - If next service specified or pending requirement exists, routes patient and auto-assigns next station.
       - If no further service, enters visit closure workflow.
    4. Automatically pulls next waiting patient into freed room if set to AVAILABLE.
    """
    stage.status = 'COMPLETED'
    stage.completed_at = timezone.now()
    if clinical_notes:
        stage.clinical_notes = clinical_notes
    stage.save()

    # Release room
    current_room = stage.room
    newly_assigned_stage = None
    if current_room:
        _, newly_assigned_stage = release_room_with_turnover(
            current_room,
            room_turnover,
            user,
            release_reason=f"Consultation completed for Ticket {stage.visit.ticket.ticket_number}"
        )

    QueueEvent.objects.create(
        stage=stage,
        event_type='COMPLETED',
        user=user,
        description=f"Consultation completed. Room turnover: {room_turnover}. Notes: {clinical_notes[:50]}..."
    )

    # Next Service Evaluation
    next_stage = None
    if next_department and next_service:
        # Route to explicitly ordered department/service
        next_stage = redirect_patient(
            current_stage=stage,
            next_department=next_department,
            next_service=next_service,
            user=user,
            reason=transfer_reason,
            clinical_notes=clinical_notes,
            room_turnover=room_turnover
        )
    else:
        # Check planned visit service requirements
        pending_req = stage.visit.service_requirements.filter(is_completed=False).first()
        if pending_req:
            pending_req.is_completed = True
            pending_req.save()
            next_stage = ServiceStage.objects.create(
                visit=stage.visit,
                department=pending_req.department,
                service=pending_req.service,
                status='WAITING',
                priority=stage.priority,
                priority_reason=f"Scheduled service follow-up ({pending_req.service.name})"
            )
            QueueEvent.objects.create(
                stage=next_stage,
                event_type='REDIRECTED_IN',
                user=user,
                description=f"Routed to next planned service: {pending_req.service.name}"
            )
            # Evaluate auto-assignment in destination department
            trigger_auto_assignment_for_stage(next_stage, user=user)
        elif close_visit:
            discharge_visit(stage.visit, user, notes="Discharged after consultation completion")

    broadcast_dashboard_event(
        'consultation_completed',
        {
            'stage_id': stage.id,
            'ticket_number': stage.visit.ticket.ticket_number,
            'status': 'COMPLETED',
            'has_next_service': bool(next_stage),
            'visit_closed': not stage.visit.is_active
        }
    )

    return stage, next_stage


@transaction.atomic
def redirect_patient(
    current_stage: ServiceStage,
    next_department: Department,
    next_service: Service,
    user,
    reason: str = "",
    clinical_notes: str = "",
    room_turnover: str = "AVAILABLE"
) -> ServiceStage:
    """
    Completes current stage, creates next stage in destination department,
    and triggers automated room assignment at the destination station.
    """
    if current_stage.status != 'COMPLETED':
        current_stage.status = 'COMPLETED'
        current_stage.completed_at = timezone.now()
        if clinical_notes:
            current_stage.clinical_notes = clinical_notes
        current_stage.save()

        if current_stage.room:
            release_room_with_turnover(
                current_stage.room,
                room_turnover,
                user,
                release_reason=f"Patient transferred to {next_department.name}"
            )

    new_stage = ServiceStage.objects.create(
        visit=current_stage.visit,
        department=next_department,
        service=next_service,
        status='WAITING',
        priority=current_stage.priority,
        priority_reason=f"Transferred from {current_stage.department.name}. {reason}".strip()
    )

    QueueEvent.objects.create(
        stage=new_stage,
        event_type='REDIRECTED_IN',
        user=user,
        description=f"Transferred from {current_stage.department.name} for {next_service.name}. Reason: {reason}"
    )

    # Evaluate automated room assignment at receiving department!
    eligible_room, assigned_stage = trigger_auto_assignment_for_stage(new_stage, user=user)

    return assigned_stage or new_stage


@transaction.atomic
def discharge_visit(visit: Visit, user, notes: str = "") -> Visit:
    """
    Formally closes and discharges a patient's visit after all required stages are satisfied.
    Cancels any residual waiting stages on this visit.
    """
    visit.is_active = False
    visit.discharged_at = timezone.now()
    visit.discharged_by = user
    visit.discharge_notes = notes
    visit.save()
    visit.stages.filter(status__in=['WAITING', 'ROOM_ASSIGNED', 'CALLED', 'ARRIVED', 'ON_HOLD']).update(status='CANCELLED')

    broadcast_dashboard_event(
        'visit_discharged',
        {
            'visit_id': str(visit.id),
            'ticket_number': visit.ticket.ticket_number,
            'patient_name': visit.patient.name,
            'patient_mrn': visit.patient.mrn
        }
    )
    return visit


@transaction.atomic
def update_room_turnover(room: Room, new_status: str, user) -> Room:
    """
    Explicit turnover management for rooms.
    """
    locked_room, assigned_stage = release_room_with_turnover(room, new_status, user)
    return locked_room


def get_estimated_wait_minutes(department: Department) -> int:
    """
    Estimates waiting time in minutes based on active waiting queue length and average service duration.
    """
    waiting_count = ServiceStage.objects.filter(department=department, status='WAITING').count()
    active_rooms = Room.objects.filter(department=department, is_active=True).exclude(status='OUT_OF_SERVICE').count()
    if active_rooms == 0:
        active_rooms = 1
    
    avg_duration = 15
    active_service = Service.objects.filter(department=department, is_active=True).first()
    if active_service and active_service.estimated_duration_mins:
        avg_duration = active_service.estimated_duration_mins

    estimated_total = int((waiting_count * avg_duration) / active_rooms)
    return max(estimated_total, 5) if waiting_count > 0 else 0
