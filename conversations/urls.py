from django.urls import path
from . import views
from . import views_auth
from . import views_motions
from . import views_runner
from . import views_settings
from . import views_admin

urlpatterns = [
    path('motions/', views_motions.motions_page, name='motions'),
    path('motions/settings/', views_settings.settings_page, name='motion_settings'),
    path('motions/<slug:slug>/', views_motions.motions_page, name='motion'),
    path('motions/login/<str:code>/', views_auth.login_page, name='motion_login'),
    path('api/auth/challenge/', views_auth.api_challenge, name='auth_challenge'),
    path('api/auth/enroll/', views_auth.api_enroll, name='auth_enroll'),
    path('api/auth/me/', views_auth.api_me, name='auth_me'),
    path('api/auth/logout/', views_auth.api_logout, name='auth_logout'),
    path('api/motions/<slug:slug>/say/', views_auth.api_say, name='api_motion_say'),
    path('api/motions/', views_motions.api_motions, name='api_motions'),
    path('api/motions/pulse/', views_motions.api_pulse, name='api_motions_pulse'),
    path('api/settings/', views_settings.api_settings, name='api_settings'),
    path('api/auth/admin/', views_admin.api_admin, name='api_auth_admin'),
    path('api/motions/<slug:slug>/turns/', views_motions.api_motion_turns, name='api_motion_turns'),
    path('api/motions/<slug:slug>/sessions/', views_motions.api_motion_sessions, name='api_motion_sessions'),
    path('api/motions/<slug:slug>/steps/<str:step_id>/', views_motions.api_motion_step, name='api_motion_step'),
    path('api/motions/<slug:slug>/typing/', views_motions.api_typing, name='api_motion_typing'),
    path('api/motions/<slug:slug>/media/', views_auth.api_media, name='api_motion_media'),
    path('api/motions/<slug:slug>/stream/', views_runner.api_stream, name='api_motion_stream'),
    path('api/motions/<slug:slug>/quiet/', views_runner.api_quiet, name='api_motion_quiet'),
    path('motions/media/<str:sha256>.<str:ext>', views_motions.media_file, name='motion_media'),
    path('api/wikilinks/', views_motions.api_wikilinks, name='api_wikilinks'),
    path('api/mentions/<slug:name>/', views_motions.api_mentions, name='api_mentions'),
    path('memory_lane/', views.memory_lane, name='memory_lane'),
    path('spy/', views.stream, name='spy'),
    path('api/recent_messages/', views.recent_messages, name='recent_messages'),
    path('api/messages/', views.api_messages, name='api_messages'),
    path('api/all_messages/', views.all_messages, name='all_messages'),
    path('api/heap_metadata/', views.heap_metadata, name='heap_metadata'),
    path('api/heap_messages/<str:heap_id>/', views.heap_messages, name='heap_messages'),
    path('api/messages_since/<str:message_id>/', views.messages_since, name='messages_since'),
    path('api/ingest/', views.ingest, name='ingest'),
]
