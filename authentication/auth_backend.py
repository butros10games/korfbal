from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model
from django.shortcuts import redirect
from django.urls import reverse

class EmailOrUsernameModelBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        UserModel = get_user_model()
        
        # Check if '@' symbol is present in the username
        if '@' in username:
            try:
                # Perform a case-insensitive lookup for email
                user = UserModel.objects.get(email__iexact=username)
            except UserModel.DoesNotExist:
                return None
        else:
            try:
                # Perform a case-insensitive lookup for username
                user = UserModel.objects.get(username__iexact=username)
            except UserModel.DoesNotExist:
                return None

        if user.check_password(password):
            return user
        return None

    def get_user(self, user_id):
        UserModel = get_user_model()
        try:
            return UserModel.objects.get(pk=user_id)
        except UserModel.DoesNotExist:
            return None
        
class BlockAdminLoginMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # If the path is the admin login and the user isn't already authenticated:
        if request.path == reverse('admin:login') and not request.user.is_authenticated:
            # Redirect to your site's main page or the login page of your own system
            return redirect('login')
        
        response = self.get_response(request)
        return response