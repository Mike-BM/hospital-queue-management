from django import forms
from .models import Patient, Service, Department, Room, ServiceStage

class KioskRegistrationForm(forms.Form):
    name = forms.CharField(
        max_length=200,
        label="Full Name",
        widget=forms.TextInput(attrs={'class': 'form-control form-control-lg', 'placeholder': 'e.g. Brian'})
    )
    phone_number = forms.CharField(
        max_length=20,
        label="Phone Number",
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control form-control-lg', 'placeholder': 'e.g. 0712 345 678'})
    )
    service = forms.ModelChoiceField(
        queryset=Service.objects.filter(is_active=True),
        label="Select Service",
        widget=forms.Select(attrs={'class': 'form-select form-select-lg'})
    )


class ReceptionRegistrationForm(forms.ModelForm):
    existing_patient_id = forms.UUIDField(required=False, widget=forms.HiddenInput())
    service = forms.ModelChoiceField(
        queryset=Service.objects.filter(is_active=True),
        label="Initial Service",
        widget=forms.Select(attrs={'class': 'form-select'})
    )
    is_appointment = forms.BooleanField(required=False, label="Scheduled Appointment", widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))
    priority = forms.ChoiceField(
        choices=ServiceStage.PRIORITY_CHOICES,
        initial=0,
        label="Initial Priority",
        widget=forms.Select(attrs={'class': 'form-select'})
    )
    priority_reason = forms.CharField(
        required=False,
        max_length=255,
        label="Priority Reason",
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Required if Urgent or Emergency'})
    )

    class Meta:
        model = Patient
        fields = ['name', 'phone_number', 'mrn', 'date_of_birth', 'gender']
        widgets = {
            'mrn': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Leave blank to auto-generate'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Full legal name'}),
            'phone_number': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 0712 345 678'}),
            'date_of_birth': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'gender': forms.Select(attrs={'class': 'form-select'}),
        }


class TriageForm(forms.Form):
    priority = forms.ChoiceField(
        choices=ServiceStage.PRIORITY_CHOICES,
        label="Clinical Priority Level",
        widget=forms.Select(attrs={'class': 'form-select form-select-lg'})
    )
    priority_reason = forms.CharField(
        required=False,
        max_length=255,
        label="Priority Escalation Reason",
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Severe chest pain, SpO2 88%'})
    )
    triage_notes = forms.CharField(
        required=False,
        label="Triage Assessment & Vitals",
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'BP, Pulse, Temperature, SpO2, Chief complaint...'})
    )


class ConsultationCompleteForm(forms.Form):
    clinical_notes = forms.CharField(
        required=False,
        label="Clinical Assessment & Diagnosis",
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'Diagnosis, treatment given, prescriptions, orders...'})
    )
    room_turnover = forms.ChoiceField(
        choices=[('AVAILABLE', 'Available Immediately'), ('AWAITING_CLEANING', 'Needs Cleaning / Turnover')],
        initial='AVAILABLE',
        label="Room Turnover Status",
        widget=forms.Select(attrs={'class': 'form-select'})
    )
    close_visit = forms.BooleanField(
        required=False,
        label="Discharge Patient (Complete Hospital Visit)",
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'})
    )


class RedirectPatientForm(forms.Form):
    next_department = forms.ModelChoiceField(
        queryset=Department.objects.filter(is_active=True),
        label="Destination Department",
        widget=forms.Select(attrs={'class': 'form-select'})
    )
    next_service = forms.ModelChoiceField(
        queryset=Service.objects.filter(is_active=True),
        label="Destination Service",
        widget=forms.Select(attrs={'class': 'form-select'})
    )
    reason = forms.CharField(
        required=False,
        max_length=255,
        label="Transfer Reason / Order Details",
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Complete Blood Count, Dispense Amoxicillin'})
    )
    clinical_notes = forms.CharField(
        required=False,
        label="Clinical Summary for Receiving Station",
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 2})
    )
    room_turnover = forms.ChoiceField(
        choices=[('AVAILABLE', 'Available Immediately'), ('AWAITING_CLEANING', 'Needs Cleaning / Turnover')],
        initial='AVAILABLE',
        label="Current Room Turnover",
        widget=forms.Select(attrs={'class': 'form-select'})
    )
