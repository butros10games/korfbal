from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    path('register/', views.register_page, name='register'),
    path('confirmation_sent/', views.confirmation_sent, name='confirmation_sent'),
    
    path('login/', views.login_page, name='login'),
    path('logout', views.logout_user, name='logout'),
    path('activate/<uidb64>/<token>/', views.activate_account, name='activate'),
    
    path('password_reset/', auth_views.PasswordResetView.as_view(template_name='authentication/registration/password_reset_form.html'), name='password_reset'),
    path('password_reset/done/', auth_views.PasswordResetDoneView.as_view(template_name='authentication/registration/password_reset_done.html'), name='password_reset_done'),
    path('reset/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(template_name='authentication/registration/password_reset_confirm.html'), name='password_reset_confirm'),
    path('reset/done/', auth_views.PasswordResetCompleteView.as_view(template_name='authentication/registration/password_reset_complete.html'), name='password_reset_complete'),
    
    path('resend-confirmation/<str:email>/', views.resend_confirmation_email, name='resend_confirmation'),
    
    path('enter_2fa_code/', views.enter_2fa_code, name='enter_2fa_code'),
    path('verify_2fa_code/', views.verify_2fa_code, name='verify_2fa_code'),
]