from django.shortcuts import render, redirect
from authentication.forms import CreateUserForm
from django.contrib import messages
from django.conf import settings
from django.urls import reverse

from django.contrib.auth import authenticate, login, logout, get_user_model
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.middleware.csrf import get_token
from django.views.decorators.csrf import csrf_exempt

from django.contrib.sites.shortcuts import get_current_site
from django.template.loader import render_to_string
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes
from django.contrib.auth.tokens import default_token_generator

from django.contrib.auth.models import User
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

from django.shortcuts import get_object_or_404
from django.contrib.auth import BACKEND_SESSION_KEY

import random
from .models import UserProfile
from game_tracker.models import Player

from django_ratelimit.decorators import ratelimit

def generate_2fa_code():
    return str(random.randint(100000, 999999))

@ratelimit(key='ip', rate='5/m', method='POST')
@csrf_exempt
def register_page(request):
    if getattr(request, 'limited', False):
        messages.error(request, 'Too many registration attempts. Please wait a minute and try again.')
        return redirect('register')
    
    form = CreateUserForm()
    
    if request.method == 'POST':
        form = CreateUserForm(request.POST)
        
        if form.is_valid():
            user = form.save(commit=False)
            user.is_active = False  # User is inactive until email is confirmed
            user.save()
            
            UserProfile_data = UserProfile.objects.create(user = user)
            UserProfile_data.save()
            
            player_data = Player.objects.create(user = user)
            player_data.save()

            # Generate token and send confirmation email
            token = default_token_generator.make_token(user)
            current_site = get_current_site(request)
            activation_url = reverse('activate', kwargs={'uidb64': urlsafe_base64_encode(force_bytes(user.pk)), 'token': token})
            activation_url = f"{request.scheme}://{current_site.domain}{activation_url}"
            
            mail_subject = 'Activate your account'
            message = render_to_string(
                'authentication/email_template/activation_email.html',
                {
                    'user': user,
                    'activation_url': activation_url,
                },
            )
            plain_message = strip_tags(message)  # Get the plain text version of the email content

            email = EmailMultiAlternatives(mail_subject, plain_message, 'butrosgroot@gmail.com', [user.email])
            email.attach_alternative(message, 'text/html')  # Attach the HTML content
            email.send()
            
            messages.success(request, 'Account created. Please check your email for activation instructions.')
            return redirect('login')
    
    context = {
        'form': form,
        'device': request.device,
        'csrftoken': get_token(request),
    }
    return render(request, 'authentication/register.html', context)

@ratelimit(key='ip', rate='2/m', method='ALL')
def resend_confirmation_email(request, email):
    if getattr(request, 'limited', False):
        messages.error(request, 'Too many confirmation resent email. Please wait a minute and try again.')
        return redirect('login')
    
    user = get_object_or_404(User, email=email)
    
    if not user.is_active:
        token = default_token_generator.make_token(user)
        current_site = get_current_site(request)
        activation_url = reverse('activate', kwargs={'uidb64': urlsafe_base64_encode(force_bytes(user.pk)), 'token': token})
        activation_url = f"{request.scheme}://{current_site.domain}{activation_url}"

        mail_subject = 'Activate your account'
        message = render_to_string(
            'authentication/email_template/activation_email.html',
            {
                'user': user,
                'activation_url': activation_url,
            },
        )
        plain_message = strip_tags(message)

        email = EmailMultiAlternatives(mail_subject, plain_message, 'butrosgroot@gmail.com', [user.email])
        email.attach_alternative(message, 'text/html')
        email.send()
        
        messages.success(request, f'Confirmation email has been resent to {user.email}.')
    else:
        messages.info(request, f'{user.email} is already confirmed.')

    return redirect('login')

@csrf_exempt
def login_page(request):    
    if request.method == 'POST':
        username = request.POST['username']
        password = request.POST['password']
        remember_me = request.POST.get('remember', False)  # Assuming the checkbox name is 'remember_me'

        user = authenticate(request, username=username, password=password)
        if user is not None:
            if user.is_active and UserProfile.objects.get(user=user).is_2fa_enabled:
                code = generate_2fa_code()
                request.session['2fa_code'] = code
                request.session['user_id_2fa'] = user.id  # Store the user's ID in the session
                request.session[BACKEND_SESSION_KEY] = 'authentication.auth_backend.EmailOrUsernameModelBackend'
                send_2fa_email(request, user.email, code)
                return redirect('enter_2fa_code')
            
            elif user.is_active:
                login(request, user)
                if not remember_me:
                    # Set the session to expire when the user closes the browser
                    request.session.set_expiry(0)
                return redirect('index')  # Redirect to the home page or any other desired page
            
            else:
                error_message = (
                    "Your account is not yet activated."
                    "<br> Please activate your account by clicking the link sent to your email."
                    "<br>"
                    "<br>If you didn't get an email "
                    "<a href='{}' style='color: #24A0ED'>Resend email</a>".format(
                        reverse('resend_confirmation', kwargs={'email': user.email})
                    )
                )
        else:
            error_message = ("Invalid login credentials."
                "</br> Please check if your username/email or passport is correct." 
                "</br> If you don't have an account, please register.")
    else:
        error_message = None
        username = ''

    context = {
        'device': request.device,
        'csrftoken': get_token(request),
        'error_message': error_message,
        'username': username,
    }

    return render(request, 'authentication/login.html', context)


@login_required(login_url = 'login')
def logout_user(request):
    logout(request)
    return redirect('login')

def activate_account(request, uidb64, token):
    try:
        uid = urlsafe_base64_decode(uidb64).decode('utf-8')
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None

    if user and default_token_generator.check_token(user, token):
        user.is_active = True
        user.save()
        messages.success(request, 'Your account has been activated. You can now log in.')
    else:
        messages.error(request, 'Activation link is invalid or has expired.')

    return redirect('login')  # Redirect to your login page

@ratelimit(key='ip', rate='3/m', method='ALL')
def send_2fa_email(request, email, code):
    if getattr(request, 'limited', False):
        messages.error(request, 'Too many 2fa login attempts. Please wait a minute and try again.')
        return redirect('login')
    
    mail_subject = 'Your 2FA Code'
    message = render_to_string(
        'authentication/email_template/2fa_email.html',
        {
            'code': code,
        },
    )
    plain_message = strip_tags(message)
    email_obj = EmailMultiAlternatives(mail_subject, plain_message, 'butrosgroot@gmail.com', [email])
    email_obj.attach_alternative(message, 'text/html')
    email_obj.send()

def enter_2fa_code(request):
    if request.method == 'POST':
        return redirect('verify_2fa_code')
    context = {
        'device': request.device,
        'csrftoken': get_token(request),
    }
    return render(request, 'authentication/enter_2fa_code.html', context)

def verify_2fa_code(request):
    if request.method == 'POST':
        entered_code = request.POST.get('code')
        session_code = request.session.get('2fa_code')
        
        if entered_code == session_code:
            user_id = request.session.get('user_id_2fa')
            if not user_id:
                messages.error(request, "There was an error verifying your 2FA code. Please try logging in again.")
                return redirect('login')

            user = User.objects.get(id=user_id)
            backend = request.session.get(BACKEND_SESSION_KEY)
            if not backend:
                messages.error(request, "There was an error verifying your 2FA code. Please try logging in again.")
                return redirect('login')

            user.backend = backend
            login(request, user)
            return redirect('index')
        else:
            messages.error(request, 'Invalid 2FA code. Please try again.')
            return redirect('enter_2fa_code')
        