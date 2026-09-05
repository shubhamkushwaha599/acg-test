from django.urls import path

from . import views

event_book_urlpatterns = [
    path('<int:event_id>/book/', views.BookEventView.as_view(), name='event-book'),
]

booking_urlpatterns = [
    path('', views.BookingListView.as_view(), name='booking-list'),
    path('<int:pk>/cancel/', views.BookingCancelView.as_view(), name='booking-cancel'),
]
