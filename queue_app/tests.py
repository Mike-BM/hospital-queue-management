import json
from datetime import date, timedelta
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.utils import timezone
from django.db import transaction

from .models import (
    Patient, Department, Service, Room, Visit,
    QueueTicket, ServiceStage, QueueEvent, TicketCounter, StaffProfile,
    RoomAssignmentLog, VisitServiceRequirement
)
from .services import (
    generate_ticket_number,
    register_patient_visit,
    triage_stage,
    call_next_ticket,
    recall_ticket,
    mark_patient_arrived,
    start_consultation,
    place_on_hold,
    resume_from_hold,
    skip_patient,
    complete_consultation,
    redirect_patient,
    discharge_visit,
    update_room_turnover,
    get_estimated_wait_minutes,
    find_eligible_room,
    assign_room_to_stage,
    trigger_auto_assignment_for_stage,
    trigger_auto_assignment_for_room,
    release_room_with_turnover,
    manual_override_room_assignment
)
from .consumers import QueueDisplayConsumer, QueueDashboardConsumer

class AutomatedPatientFlowAndRoomAssignmentTests(TestCase):
    def setUp(self):
        # 1. Departments & Services
        self.dept_consult = Department.objects.create(name="Consultation", prefix="C")
        self.dept_lab = Department.objects.create(name="Laboratory", prefix="L")
        self.dept_pharmacy = Department.objects.create(name="Pharmacy", prefix="P")

        self.service_general = Service.objects.create(
            department=self.dept_consult, name="General Consultation", estimated_duration_mins=15
        )
        self.service_blood = Service.objects.create(
            department=self.dept_lab, name="Blood Test", estimated_duration_mins=10
        )
        self.service_dispense = Service.objects.create(
            department=self.dept_pharmacy, name="Dispensing", estimated_duration_mins=5
        )

        # 2. Rooms
        self.room_1 = Room.objects.create(department=self.dept_consult, name="Room 101", status='AVAILABLE')
        self.room_2 = Room.objects.create(department=self.dept_consult, name="Room 102", status='AVAILABLE')
        self.lab_room = Room.objects.create(department=self.dept_lab, name="Lab Station A", status='AVAILABLE')

        # 3. Staff Users & Profiles
        self.admin_user = User.objects.create_superuser(username="admin_user", password="password123")
        
        self.doctor_user = User.objects.create_user(username="dr_brian", password="password123")
        StaffProfile.objects.create(user=self.doctor_user, role='CLINICAL', department=self.dept_consult)
        self.room_1.assigned_staff = self.doctor_user
        self.room_1.save()

        self.receptionist_user = User.objects.create_user(username="receptionist_ann", password="password123")
        StaffProfile.objects.create(user=self.receptionist_user, role='RECEPTION')

        self.triage_nurse = User.objects.create_user(username="nurse_sarah", password="password123")
        StaffProfile.objects.create(user=self.triage_nurse, role='TRIAGE', department=self.dept_consult)

        # 4. Patients
        self.patient_1 = Patient.objects.create(
            name="Brian Muema",
            phone_number="0793706054",
            gender="M",
            date_of_birth=date(1995, 5, 20)
        )
        self.patient_2 = Patient.objects.create(
            name="Jane Doe",
            phone_number="0711223344",
            gender="F",
            date_of_birth=date(1998, 8, 14)
        )
        self.patient_3 = Patient.objects.create(
            name="Alice Smith",
            phone_number="0722334455",
            gender="F",
            date_of_birth=date(1990, 1, 10)
        )

    # -------------------------------------------------------------
    # 1. Patient finishes triage & automatically assigned available room
    # -------------------------------------------------------------
    def test_01_auto_assign_after_triage(self):
        # Register patient into queue
        visit, stage, _ = register_patient_visit(
            patient=self.patient_1,
            initial_department=self.dept_consult,
            initial_service=self.service_general,
            user=self.receptionist_user
        )
        # Stage is triaged
        triaged_stage = triage_stage(
            stage=stage,
            priority=1,
            triage_notes="Temp 38.5C, mild tachypnea",
            user=self.triage_nurse,
            priority_reason="Moderate fever"
        )
        triaged_stage.refresh_from_db()
        self.assertEqual(triaged_stage.status, 'ROOM_ASSIGNED')
        self.assertIsNotNone(triaged_stage.room)
        self.assertEqual(triaged_stage.room.status, 'OCCUPIED')
        self.assertIsNotNone(triaged_stage.assigned_at)
        self.assertEqual(triaged_stage.assignment_mode, 'AUTO')

    # -------------------------------------------------------------
    # 2. Consultation Room 1 is occupied, so engine assigns Room 2
    # -------------------------------------------------------------
    def test_02_assign_another_eligible_room_when_first_occupied(self):
        # Occupy Room 101 with Patient 1
        visit1, stage1, _ = register_patient_visit(self.patient_1, self.dept_consult, self.service_general)
        assign_room_to_stage(stage1, self.room_1, user=self.doctor_user, mode='AUTO')
        self.room_1.refresh_from_db()
        self.assertEqual(self.room_1.status, 'OCCUPIED')

        # Patient 2 completes triage
        visit2, stage2, _ = register_patient_visit(self.patient_2, self.dept_consult, self.service_general)
        triage_stage(stage2, priority=0, triage_notes="Routine checkup", user=self.triage_nurse)
        
        stage2.refresh_from_db()
        self.assertEqual(stage2.status, 'ROOM_ASSIGNED')
        self.assertEqual(stage2.room.id, self.room_2.id) # Successfully picked Room 102!
        self.room_2.refresh_from_db()
        self.assertEqual(self.room_2.status, 'OCCUPIED')

    # -------------------------------------------------------------
    # 3. All eligible rooms occupied -> patient remains waiting
    # -------------------------------------------------------------
    def test_03_patient_remains_waiting_when_all_rooms_occupied(self):
        # Occupy both Room 101 and Room 102
        v1, s1, _ = register_patient_visit(self.patient_1, self.dept_consult, self.service_general)
        assign_room_to_stage(s1, self.room_1, user=self.doctor_user)
        v2, s2, _ = register_patient_visit(self.patient_2, self.dept_consult, self.service_general)
        assign_room_to_stage(s2, self.room_2, user=self.admin_user)

        # Patient 3 is registered and triaged
        v3, s3, _ = register_patient_visit(self.patient_3, self.dept_consult, self.service_general)
        triaged_s3 = triage_stage(s3, priority=1, triage_notes="Urgent headache", user=self.triage_nurse)
        
        triaged_s3.refresh_from_db()
        self.assertEqual(triaged_s3.status, 'WAITING') # Retained in waiting queue
        self.assertIsNone(triaged_s3.room)

    # -------------------------------------------------------------
    # 4. Room becomes available, allowing eligible waiting patient to be assigned
    # -------------------------------------------------------------
    def test_04_room_becomes_available_pulls_waiting_patient(self):
        # Setup: Room 101 occupied by P1, P2 is WAITING
        v1, s1, _ = register_patient_visit(self.patient_1, self.dept_consult, self.service_general)
        assign_room_to_stage(s1, self.room_1, user=self.doctor_user)
        
        v2, s2, _ = register_patient_visit(self.patient_2, self.dept_consult, self.service_general)
        s2.status = 'WAITING'
        s2.save()

        # Doctor completes consultation on P1 and releases Room 101 as AVAILABLE
        complete_consultation(stage=s1, user=self.doctor_user, room_turnover='AVAILABLE')

        # Engine should have automatically pulled waiting Patient 2 into Room 101!
        s2.refresh_from_db()
        self.assertEqual(s2.status, 'ROOM_ASSIGNED')
        self.assertEqual(s2.room.id, self.room_1.id)
        self.room_1.refresh_from_db()
        self.assertEqual(self.room_1.status, 'OCCUPIED')

    # -------------------------------------------------------------
    # 5. Patient assignment immediately updates receptionist, doctor, and patient display
    # -------------------------------------------------------------
    def test_05_assignment_updates_audit_and_broadcasting(self):
        v, s, _ = register_patient_visit(self.patient_1, self.dept_consult, self.service_general)
        assigned_stage = assign_room_to_stage(s, self.room_1, user=self.doctor_user, mode='AUTO')

        # Check RoomAssignmentLog audit
        log = RoomAssignmentLog.objects.filter(stage=assigned_stage).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.room, self.room_1)
        self.assertEqual(log.assignment_mode, 'AUTO')

        # Check QueueEvent
        event = QueueEvent.objects.filter(stage=assigned_stage, event_type='ROOM_ASSIGNED').first()
        self.assertIsNotNone(event)
        self.assertIn("Room 101", event.description)

    # -------------------------------------------------------------
    # 6. Doctor completes consultation and patient is routed to next required service
    # -------------------------------------------------------------
    def test_06_complete_consultation_routes_to_next_service(self):
        v, s, _ = register_patient_visit(self.patient_1, self.dept_consult, self.service_general)
        assign_room_to_stage(s, self.room_1, user=self.doctor_user)
        start_consultation(s, self.doctor_user)

        # Doctor orders Laboratory service on completion
        completed_stage, lab_stage = complete_consultation(
            stage=s,
            user=self.doctor_user,
            clinical_notes="Patient presents with high fever, order blood tests.",
            room_turnover='AVAILABLE',
            next_department=self.dept_lab,
            next_service=self.service_blood,
            transfer_reason="Suspected infection"
        )

        completed_stage.refresh_from_db()
        self.assertEqual(completed_stage.status, 'COMPLETED')
        self.assertIsNotNone(lab_stage)
        self.assertEqual(lab_stage.department, self.dept_lab)
        self.assertEqual(lab_stage.visit.id, v.id)
        # Lab Station A was AVAILABLE, so patient is auto-assigned to Lab Station A!
        self.assertEqual(lab_stage.status, 'ROOM_ASSIGNED')
        self.assertEqual(lab_stage.room.id, self.lab_room.id)

    # -------------------------------------------------------------
    # 7. Patient requiring no further service enters appropriate visit-closure workflow
    # -------------------------------------------------------------
    def test_07_no_further_service_eligible_for_discharge(self):
        v, s, _ = register_patient_visit(self.patient_1, self.dept_consult, self.service_general)
        assign_room_to_stage(s, self.room_1, user=self.doctor_user)
        start_consultation(s, self.doctor_user)

        # Doctor completes consultation with close_visit=True
        completed_stage, next_stage = complete_consultation(
            stage=s,
            user=self.doctor_user,
            clinical_notes="Prescription given. Patient advised to rest.",
            room_turnover='AVAILABLE',
            close_visit=True
        )

        self.assertIsNone(next_stage)
        v.refresh_from_db()
        self.assertFalse(v.is_active)
        self.assertIsNotNone(v.discharged_at)

    # -------------------------------------------------------------
    # 8. Room requiring cleaning is not assigned to another patient
    # -------------------------------------------------------------
    def test_08_room_requiring_cleaning_is_not_assigned(self):
        v1, s1, _ = register_patient_visit(self.patient_1, self.dept_consult, self.service_general)
        assign_room_to_stage(s1, self.room_1, user=self.doctor_user)

        # Doctor completes consultation and marks room turnover as AWAITING_CLEANING
        complete_consultation(stage=s1, user=self.doctor_user, room_turnover='AWAITING_CLEANING')

        self.room_1.refresh_from_db()
        self.assertEqual(self.room_1.status, 'AWAITING_CLEANING')

        # Also make Room 102 UNAVAILABLE
        self.room_2.status = 'UNAVAILABLE'
        self.room_2.save()

        # Patient 2 completes triage
        v2, s2, _ = register_patient_visit(self.patient_2, self.dept_consult, self.service_general)
        triaged_s2 = triage_stage(s2, priority=0, triage_notes="Checkup", user=self.triage_nurse)
        
        triaged_s2.refresh_from_db()
        self.assertEqual(triaged_s2.status, 'WAITING')
        self.assertIsNone(triaged_s2.room) # Room 101 was NOT assigned because cleaning is required!

    # -------------------------------------------------------------
    # 9. Two simultaneous assignment requests cannot reserve the same exclusive room
    # -------------------------------------------------------------
    def test_09_concurrency_prevention_double_room_assignment(self):
        v1, s1, _ = register_patient_visit(self.patient_1, self.dept_consult, self.service_general)
        v2, s2, _ = register_patient_visit(self.patient_2, self.dept_consult, self.service_general)

        # First assignment succeeds
        assign_room_to_stage(s1, self.room_1, user=self.doctor_user)
        self.room_1.refresh_from_db()
        self.assertEqual(self.room_1.status, 'OCCUPIED')

        # Second assignment targeting the same room must be rejected
        with self.assertRaises(ValueError):
            assign_room_to_stage(s2, self.room_1, user=self.admin_user)

    # -------------------------------------------------------------
    # 10. Network interruption reconciliation (initial state snapshot)
    # -------------------------------------------------------------
    async def test_10_network_interruption_reconciliation(self):
        v, s, _ = register_patient_visit(self.patient_1, self.dept_consult, self.service_general)
        assign_room_to_stage(s, self.room_1, user=self.doctor_user)

        consumer = QueueDashboardConsumer()
        snapshot = await consumer.get_active_dashboard_snapshot()

        self.assertIn('active_assignments', snapshot)
        self.assertIn('rooms', snapshot)
        assigned_tickets = [a['ticket_number'] for a in snapshot['active_assignments']]
        self.assertIn(v.ticket.ticket_number, assigned_tickets)

    # -------------------------------------------------------------
    # 11. Unauthorised user cannot assign rooms or complete consultations
    # -------------------------------------------------------------
    def test_11_unauthorised_user_access_control(self):
        client = Client()
        # Unauthenticated user cannot access dashboard or action endpoints
        resp = client.post('/dashboard/call/1/')
        self.assertEqual(resp.status_code, 302) # Redirects to login

        # Non-staff patient or random user cannot trigger manual override
        unauth_user = User.objects.create_user(username="random_patient", password="password")
        client.force_login(unauth_user)
        
        v, s, _ = register_patient_visit(self.patient_1, self.dept_consult, self.service_general)
        resp = client.post(f'/dashboard/manual_assign/{s.id}/', {'room_id': self.room_1.id, 'override_reason': 'test'})
        # Blocked by role_required decorator
        self.assertEqual(resp.status_code, 302)

    # -------------------------------------------------------------
    # 12. Failed database operation does not falsely report success
    # -------------------------------------------------------------
    def test_12_failed_database_operation_atomic_rollback(self):
        v, s, _ = register_patient_visit(self.patient_1, self.dept_consult, self.service_general)
        
        # Manual override without reason should fail validation
        with self.assertRaises(ValueError):
            manual_override_room_assignment(s, self.room_1, self.doctor_user, reason="")

        # Verify state is not corrupted
        s.refresh_from_db()
        self.assertEqual(s.status, 'WAITING')
        self.assertIsNone(s.room)
        self.room_1.refresh_from_db()
        self.assertEqual(self.room_1.status, 'AVAILABLE')
