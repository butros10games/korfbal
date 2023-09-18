from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='home'),
    path('teams/', views.teams, name='teams'),
    path('profile/', views.profile, name='profile'),
]
