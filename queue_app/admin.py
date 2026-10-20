from django.contrib import admin
from .models import (
    Patient, Department, Room, Service, StaffProfile,
    Visit, QueueTicket, ServiceStage, QueueEvent, TicketCounter,
    RoomAssignmentLog, VisitServiceRequirement
)

@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = ('name', 'mrn', 'phone_number', 'date_of_birth', 'gender', 'created_at')
    search_fields = ('name', 'mrn', 'phone_number')
    list_filter = ('gender', 'created_at')

@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ('name', 'prefix', 'is_active')
    search_fields = ('name', 'prefix')

@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ('name', 'department', 'status', 'assigned_staff', 'is_active', 'last_released_at')
    list_filter = ('department', 'status', 'is_active')
    search_fields = ('name',)

@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ('name', 'department', 'estimated_duration_mins', 'is_active')
    list_filter = ('department', 'is_active')
    search_fields = ('name',)

@admin.register(StaffProfile)
class StaffProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'department')
    list_filter = ('role', 'department')
    search_fields = ('user__username', 'user__first_name', 'user__last_name')

@admin.register(Visit)
class VisitAdmin(admin.ModelAdmin):
    list_display = ('patient', 'is_appointment', 'is_active', 'registered_at', 'discharged_at')
    list_filter = ('is_appointment', 'is_active', 'registered_at')
    search_fields = ('patient__name', 'patient__mrn')

@admin.register(VisitServiceRequirement)
class VisitServiceRequirementAdmin(admin.ModelAdmin):
    list_display = ('visit', 'department', 'service', 'order', 'is_completed', 'created_at')
    list_filter = ('is_completed', 'department')

@admin.register(QueueTicket)
class QueueTicketAdmin(admin.ModelAdmin):
    list_display = ('ticket_number', 'visit', 'issued_at')
    search_fields = ('ticket_number', 'visit__patient__name', 'visit__patient__mrn')

@admin.register(ServiceStage)
class ServiceStageAdmin(admin.ModelAdmin):
    list_display = ('visit', 'department', 'service', 'room', 'status', 'priority', 'assignment_mode', 'handled_by', 'added_to_queue_at')
    list_filter = ('status', 'priority', 'assignment_mode', 'department')
    search_fields = ('visit__ticket__ticket_number', 'visit__patient__name')

@admin.register(RoomAssignmentLog)
class RoomAssignmentLogAdmin(admin.ModelAdmin):
    list_display = ('stage', 'room', 'assigned_staff', 'assignment_mode', 'assigned_at', 'released_at')
    list_filter = ('assignment_mode', 'room__department')
    search_fields = ('stage__visit__ticket__ticket_number', 'room__name', 'override_reason')

@admin.register(QueueEvent)
class QueueEventAdmin(admin.ModelAdmin):
    list_display = ('stage', 'event_type', 'user', 'timestamp')
    list_filter = ('event_type', 'timestamp')
    search_fields = ('stage__visit__ticket__ticket_number', 'description')

@admin.register(TicketCounter)
class TicketCounterAdmin(admin.ModelAdmin):
    list_display = ('date', 'prefix', 'last_number')
