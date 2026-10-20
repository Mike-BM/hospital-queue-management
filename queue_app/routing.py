from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/display/$', consumers.QueueDisplayConsumer.as_asgi()),
    re_path(r'ws/dashboard/$', consumers.QueueDashboardConsumer.as_asgi()),
]
