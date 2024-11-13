from django.urls import path
from . import views


urlpatterns = [
    path('<uuid:player_id>/', views.profile_detail, name='profile_detail'),
    
    path('api/upload_profile_picture/', views.upload_profile_picture, name='upload_profile_picture')
]
