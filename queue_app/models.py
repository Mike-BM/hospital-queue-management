import uuid
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone

class Patient(models.Model):
    GENDER_CHOICES = [
        ('M', 'Male'),
        ('F', 'Female'),
        ('O', 'Other'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    mrn = models.CharField(max_length=30, unique=True, null=True, blank=True, db_index=True, help_text="Medical Record Number / Hospital ID")
    name = models.CharField(max_length=200)
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.mrn:
            short_id = str(self.id)[:6].upper()
            year = timezone.now().year
            self.mrn = f"HOSP-{year}-{short_id}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} [{self.mrn}]"


class Department(models.Model):
    name = models.CharField(max_length=100)
    prefix = models.CharField(max_length=5, help_text="Ticket prefix, e.g., 'C' for Consultation")
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Room(models.Model):
    STATUS_CHOICES = [
        ('AVAILABLE', 'Available'),
        ('RESERVED', 'Reserved'),
        ('OCCUPIED', 'Occupied'),
        ('AWAITING_CLEANING', 'Awaiting Cleaning'),
        ('UNAVAILABLE', 'Unavailable'),
        ('OUT_OF_SERVICE', 'Out of Service'),
    ]
    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name='rooms')
    name = models.CharField(max_length=50) # e.g., "Room 101"
    is_active = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='AVAILABLE')
    assigned_staff = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_rooms')
    last_released_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp when room was last released for idle balance")

    def __str__(self):
        return f"{self.department.name} - {self.name}"


class Service(models.Model):
    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name='services')
    name = models.CharField(max_length=100) # e.g., "General Consultation"
    estimated_duration_mins = models.IntegerField(default=15)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class StaffProfile(models.Model):
    ROLE_CHOICES = [
        ('ADMIN', 'Administrator'),
        ('RECEPTION', 'Receptionist'),
        ('TRIAGE', 'Triage Nurse'),
        ('CLINICAL', 'Clinical Staff / Doctor'),
        ('LAB', 'Laboratory Staff'),
        ('PHARMACY', 'Pharmacy Staff'),
    ]
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='staff_profile')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='CLINICAL')
    department = models.ForeignKey(Department, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"{self.user.username} ({self.get_role_display()})"

    @property
    def is_admin(self):
        return self.role == 'ADMIN' or self.user.is_superuser

    @property
    def is_receptionist(self):
        return self.role == 'RECEPTION' or self.is_admin

    @property
    def is_triage(self):
        return self.role == 'TRIAGE' or self.is_admin

    @property
    def is_clinical(self):
        return self.role in ['CLINICAL', 'LAB', 'PHARMACY'] or self.is_admin


class Visit(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name='visits')
    registered_at = models.DateTimeField(auto_now_add=True)
    is_appointment = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True, help_text="True while patient has active stages; false when discharged")
    discharge_notes = models.TextField(blank=True)
    discharged_at = models.DateTimeField(null=True, blank=True)
    discharged_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='discharged_visits')

    def __str__(self):
        return f"Visit: {self.patient.name} [{self.patient.mrn}] at {self.registered_at.strftime('%Y-%m-%d %H:%M')}"


class VisitServiceRequirement(models.Model):
    """
    Tracks planned sequential clinical services for a patient's visit.
    Enables automatic routing upon consultation completion.
    """
    visit = models.ForeignKey(Visit, on_delete=models.CASCADE, related_name='service_requirements')
    department = models.ForeignKey(Department, on_delete=models.CASCADE)
    service = models.ForeignKey(Service, on_delete=models.CASCADE)
    order = models.PositiveIntegerField(default=1)
    is_completed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', 'created_at']

    def __str__(self):
        return f"Requirement: {self.department.name} - {self.service.name} (Visit: {self.visit.ticket.ticket_number if hasattr(self.visit, 'ticket') else self.visit.id})"


class QueueTicket(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    visit = models.OneToOneField(Visit, on_delete=models.CASCADE, related_name='ticket')
    ticket_number = models.CharField(max_length=20, unique=True, db_index=True)
    issued_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.ticket_number


class ServiceStage(models.Model):
    STATUS_CHOICES = [
        ('WAITING', 'Waiting'),
        ('ROOM_ASSIGNED', 'Room Assigned'),
        ('CALLED', 'Called'),
        ('ARRIVED', 'Arrived'),
        ('IN_SERVICE', 'In Service'),
        ('ON_HOLD', 'On Hold'),
        ('SKIPPED', 'Skipped'),
        ('COMPLETED', 'Completed'),
        ('CANCELLED', 'Cancelled'),
    ]
    
    PRIORITY_CHOICES = [
        (0, 'Routine'),
        (1, 'Urgent'),
        (2, 'Emergency'),
    ]

    ASSIGNMENT_MODE_CHOICES = [
        ('AUTO', 'Automated Engine'),
        ('MANUAL', 'Manual Override'),
    ]

    visit = models.ForeignKey(Visit, on_delete=models.CASCADE, related_name='stages')
    department = models.ForeignKey(Department, on_delete=models.PROTECT)
    service = models.ForeignKey(Service, on_delete=models.PROTECT)
    room = models.ForeignKey(Room, on_delete=models.SET_NULL, null=True, blank=True)
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='WAITING', db_index=True)
    priority = models.IntegerField(choices=PRIORITY_CHOICES, default=0, db_index=True)
    priority_reason = models.CharField(max_length=255, blank=True)
    
    # Timestamps
    added_to_queue_at = models.DateTimeField(auto_now_add=True)
    assigned_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp when room was assigned")
    called_at = models.DateTimeField(null=True, blank=True)
    arrived_at = models.DateTimeField(null=True, blank=True)
    consultation_started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    
    handled_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='handled_stages')
    assignment_mode = models.CharField(max_length=20, choices=ASSIGNMENT_MODE_CHOICES, default='AUTO')
    override_reason = models.CharField(max_length=255, blank=True, help_text="Reason for manual override")
    
    # Clinical and Triage tracking
    triage_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='triaged_stages')
    triage_notes = models.TextField(blank=True, help_text="Vitals, observations, symptoms")
    clinical_notes = models.TextField(blank=True, help_text="Clinical findings, diagnosis, instructions")
    hold_reason = models.CharField(max_length=255, blank=True)
    skip_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-priority', 'added_to_queue_at']
        indexes = [
            models.Index(fields=['department', 'status']),
        ]

    def __str__(self):
        return f"{self.visit.ticket.ticket_number} - {self.department.name} ({self.get_status_display()})"


class RoomAssignmentLog(models.Model):
    """
    Immutable audit trail for all room assignments and releases.
    """
    stage = models.ForeignKey(ServiceStage, on_delete=models.CASCADE, related_name='assignment_logs')
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name='assignment_logs')
    assigned_staff = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='assignment_logs')
    assignment_mode = models.CharField(max_length=20, choices=ServiceStage.ASSIGNMENT_MODE_CHOICES, default='AUTO')
    override_reason = models.CharField(max_length=255, blank=True)
    assigned_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)
    release_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-assigned_at']

    def __str__(self):
        return f"Assignment: {self.stage.visit.ticket.ticket_number} -> {self.room.name} ({self.assignment_mode}) at {self.assigned_at}"


class QueueEvent(models.Model):
    stage = models.ForeignKey(ServiceStage, on_delete=models.CASCADE, related_name='events')
    event_type = models.CharField(max_length=50) # e.g., 'REGISTERED', 'ROOM_ASSIGNED', 'CALLED', 'ARRIVED', 'STARTED', 'ON_HOLD', 'SKIPPED', 'COMPLETED', 'REDIRECTED_IN'
    description = models.TextField(blank=True)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.stage.visit.ticket.ticket_number} - {self.event_type} at {self.timestamp}"


class TicketCounter(models.Model):
    date = models.DateField()
    prefix = models.CharField(max_length=5)
    last_number = models.IntegerField(default=0)

    class Meta:
        unique_together = ('date', 'prefix')
