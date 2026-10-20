from django.urls import path
from . import views

urlpatterns = [
    # Kiosk & Patient Ticketing
    path('', views.kiosk_view, name='kiosk'),
    path('ticket/<uuid:ticket_id>/', views.print_ticket_view, name='print_ticket'),
    
    # Reception Desk
    path('reception/', views.reception_view, name='reception'),
    path('reception/search/', views.reception_search_api, name='reception_search_api'),
    
    # Triage Station
    path('triage/', views.triage_view, name='triage'),
    
    # Staff / Doctor Consultation Dashboard
    path('dashboard/', views.staff_dashboard_view, name='staff_dashboard'),
    path('dashboard/assign_room/', views.assign_room_view, name='assign_room'),
    path('dashboard/call/<int:department_id>/', views.call_next_view, name='call_next'),
    path('dashboard/auto_assign/<int:department_id>/', views.trigger_auto_assign_view, name='trigger_auto_assign'),
    path('dashboard/manual_assign/<int:stage_id>/', views.manual_room_assign_view, name='manual_room_assign'),
    path('dashboard/arrive/<int:stage_id>/', views.mark_arrived_view, name='mark_arrived'),
    path('dashboard/start/<int:stage_id>/', views.start_service_view, name='start_service'),
    path('dashboard/hold/<int:stage_id>/', views.place_on_hold_view, name='place_on_hold'),
    path('dashboard/resume/<int:stage_id>/', views.resume_hold_view, name='resume_hold'),
    path('dashboard/skip/<int:stage_id>/', views.skip_patient_view, name='skip_patient'),
    path('dashboard/recall/<int:stage_id>/', views.recall_ticket_view, name='recall_ticket'),
    path('dashboard/complete/<int:stage_id>/', views.complete_consultation_view, name='complete_consultation'),
    path('dashboard/redirect/<int:stage_id>/', views.redirect_patient_view, name='redirect_patient'),
    path('dashboard/room_status/<int:room_id>/', views.update_room_status_view, name='update_room_status'),
    
    # Public Live Display
    path('display/', views.public_display_view, name='public_display'),
    
    # Reports & Analytics
    path('reports/', views.report_view, name='reports'),
]
