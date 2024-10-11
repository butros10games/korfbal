from django.urls import path
from . import views

urlpatterns = [
    path('<uuid:club_id>/', views.club_detail, name='club_detail'),
]
