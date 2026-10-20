import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from .models import ServiceStage, Room

class QueueDisplayConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for public-facing waiting room displays.
    Broadcasts now serving tickets, room assignments, and voice announcements.
    """
    async def connect(self):
        self.room_group_name = 'queue_display'
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )
        await self.accept()

        initial_state = await self.get_current_display_state()
        await self.send(text_data=json.dumps({
            'type': 'initial_state',
            **initial_state
        }))

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )

    async def queue_update(self, event):
        await self.send(text_data=json.dumps({
            'type': 'call',
            'ticket_number': event['ticket_number'],
            'department': event['department'],
            'room': event['room'],
            'status': event.get('status', 'CALLED')
        }))

    @database_sync_to_async
    def get_current_display_state(self):
        active_stages = list(ServiceStage.objects.filter(
            status__in=['ROOM_ASSIGNED', 'CALLED', 'ARRIVED', 'IN_SERVICE']
        ).select_related('visit__ticket', 'department', 'room').order_by('-called_at', '-assigned_at')[:6])

        current = None
        recent = []

        if active_stages:
            first = active_stages[0]
            current = {
                'ticket_number': first.visit.ticket.ticket_number,
                'department': first.department.name,
                'room': first.room.name if first.room else "Station"
            }
            for stage in active_stages[1:]:
                recent.append({
                    'ticket_number': stage.visit.ticket.ticket_number,
                    'department': stage.department.name,
                    'room': stage.room.name if stage.room else "Station"
                })

        return {
            'current': current,
            'recent': recent
        }


class QueueDashboardConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for staff dashboards (Reception, Doctor, Triage).
    Delivers live status changes, automatic room assignments, and room availability events.
    """
    async def connect(self):
        self.room_group_name = 'queue_dashboard'
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )
        await self.accept()

        # Send initial snapshot of active room assignments
        snapshot = await self.get_active_dashboard_snapshot()
        await self.send(text_data=json.dumps({
            'type': 'dashboard_snapshot',
            'data': snapshot
        }))

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )

    async def dashboard_event(self, event):
        # Broadcast server-side event payload directly to connected staff browsers
        await self.send(text_data=json.dumps({
            'type': event['event'],
            'data': event['data']
        }))

    @database_sync_to_async
    def get_active_dashboard_snapshot(self):
        active_stages = list(ServiceStage.objects.filter(
            status__in=['ROOM_ASSIGNED', 'CALLED', 'ARRIVED', 'IN_SERVICE', 'ON_HOLD']
        ).select_related('visit__ticket', 'visit__patient', 'department', 'room', 'handled_by'))

        rooms = list(Room.objects.all().select_related('department', 'assigned_staff'))

        return {
            'active_assignments': [{
                'stage_id': s.id,
                'ticket_number': s.visit.ticket.ticket_number,
                'patient_name': s.visit.patient.name,
                'patient_mrn': s.visit.patient.mrn,
                'department': s.department.name,
                'room': s.room.name if s.room else None,
                'room_id': s.room.id if s.room else None,
                'clinician': s.handled_by.username if s.handled_by else None,
                'status': s.status,
                'status_display': s.get_status_display(),
                'priority': s.priority,
                'assigned_at': s.assigned_at.strftime('%H:%M:%S') if s.assigned_at else None,
                'assignment_mode': s.assignment_mode,
            } for s in active_stages],
            'rooms': [{
                'id': r.id,
                'name': r.name,
                'department': r.department.name,
                'status': r.status,
                'status_display': r.get_status_display(),
                'assigned_staff': r.assigned_staff.username if r.assigned_staff else None
            } for r in rooms]
        }
