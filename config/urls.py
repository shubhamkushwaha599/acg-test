from django.contrib import admin
from django.urls import include, path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from bookings.urls import booking_urlpatterns, event_book_urlpatterns
from events.urls import urlpatterns as event_urlpatterns

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('events/', include(event_urlpatterns)),
    path('events/', include(event_book_urlpatterns)),
    path('bookings/', include(booking_urlpatterns)),
]
