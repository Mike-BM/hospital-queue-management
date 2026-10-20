from functools import wraps
from django.shortcuts import redirect
from django.contrib import messages
from .models import StaffProfile

def get_or_create_staff_profile(user):
    """
    Retrieves or establishes a StaffProfile for an authenticated user.
    Superusers automatically default to the ADMIN role.
    """
    if not hasattr(user, 'staff_profile'):
        role = 'ADMIN' if user.is_superuser else 'CLINICAL'
        StaffProfile.objects.create(user=user, role=role)
    return user.staff_profile

def role_required(allowed_roles):
    """
    Decorator for views that enforces role-based access control.
    Superusers always pass.
    """
    if isinstance(allowed_roles, str):
        allowed_roles = [allowed_roles]

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect('login')

            if request.user.is_superuser:
                return view_func(request, *args, **kwargs)

            profile = get_or_create_staff_profile(request.user)
            if profile.role in allowed_roles or profile.role == 'ADMIN':
                return view_func(request, *args, **kwargs)

            messages.error(request, f"Access restricted. Your role ({profile.get_role_display()}) is not authorized for this action.")
            return redirect('staff_dashboard')
        return _wrapped_view
    return decorator
