from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('catalog/', views.catalog, name='catalog'),
    path('previous/', views.previous_page, name='previous_page'),
    
    path('api/search/', views.search, name='api_search'),
    path('api/catalog/data', views.catalog_data, name='api_catalog_data'),
]

