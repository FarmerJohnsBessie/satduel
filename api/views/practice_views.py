"""
Infinite-practice endpoints: adaptive question selection, the free-tier daily
quota, and the revised single-player Elo.

Tier rules
----------
Free:    FREE_DAILY_LIMIT graded answers per day, random topics only.
Premium: unlimited answers, may narrow practice with the filter bar
         (topic, difficulty level, saved, done-before, and result). The
         "done-before"/"result" filters open a review pool that re-serves
         already-answered questions; reviewing is free and never moves Elo or
         the daily quota.

Elo rules (revision of the old behaviour, which updated ratings on every
check_answer call from any surface):
- Only practice-mode answers move Elo, and only the FIRST attempt a user ever
  makes at a given question (repeat sightings are for review, not rating).
- Users get a provisional K-factor (32) for their first 30 attempts, then 16.
- Question ratings move slowly (K=8) since they aggregate many users.
- Ratings are clamped to [100, 3000].
"""
import hashlib
import random

from django.conf import settings
from django.db.models import Count, F, Max, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from api import generation
from api.models import (
    DEFAULT_TEST_PREP,
    PracticeActiveQuestion,
    PracticeAttempt,
    PracticeStats,
    PracticeTestResult,
    PracticeTypeStats,
    Question,
    SavedQuestion,
)
from api.views.serializers import QuestionSerializer

# question_types are the official College Board skill names in the AI
# generator's taxonomy — reused so the two never drift apart.
DEFAULT_QUESTION_TYPES = [
    s['name'] for d in generation.ENGLISH_DOMAINS for s in d['skills']
]
MATH_QUESTION_TYPES = [
    s['name'] for d in generation.MATH_DOMAINS for s in d['skills']
]

SUBJECT_TYPES = {
    'english': DEFAULT_QUESTION_TYPES,
    'math': MATH_QUESTION_TYPES,
}
SUBJECTS = list(SUBJECT_TYPES)


def resolve_subject(name):
    return name if name in SUBJECT_TYPES else 'english'


def subject_of(question):
    """Which subject a question belongs to, from its question_type."""
    return generation.subject_of_type(question.question_type)


def subject_filter(subject):
    return Q(subject=resolve_subject(subject))


def get_practice_stats(user, subject):
    """The user's per-subject stats row (rating, counters, active question)."""
    stats, _ = PracticeStats.objects.get_or_create(
        user=user, test_prep_id=DEFAULT_TEST_PREP, subject=resolve_subject(subject),
    )
    return stats


def practice_stats_breakdown(user):
    """Per-subject lifetime counters (includes legacy pre-PracticeAttempt
    progress, which the migration folded into the English row)."""
    rows = {
        row.subject: row for row in PracticeStats.objects.filter(
            user=user, test_prep_id=DEFAULT_TEST_PREP,
        )
    }
    payload = {}
    for subject in SUBJECTS:
        row = rows.get(subject)
        answered = row.answered if row else 0
        correct = row.correct if row else 0
        payload[f'{subject}_answered'] = answered
        payload[f'{subject}_correct'] = correct
        payload[f'{subject}_accuracy'] = round(correct / answered * 100) if answered else None
        payload[f'{subject}_elo'] = row.elo if row else 1200
    payload['practice_answered'] = sum(payload[f'{s}_answered'] for s in SUBJECTS)
    payload['practice_correct'] = sum(payload[f'{s}_correct'] for s in SUBJECTS)
    payload['practice_accuracy'] = (
        round(payload['practice_correct'] / payload['practice_answered'] * 100)
        if payload['practice_answered'] else None
    )
    payload['current_streak'] = practice_current_streak(user)
    return payload


# Old name, kept so callers read naturally at both call sites.
practice_attempt_breakdown = practice_stats_breakdown


def record_practice_answer(user, question, correct, subject, selected_choice):
    """Log the attempt and bump the subject's lifetime counters atomically."""
    first_attempt = not PracticeAttempt.objects.filter(
        user=user, test_prep_id=DEFAULT_TEST_PREP, question=question,
    ).exists()
    PracticeAttempt.objects.create(
        user=user, test_prep_id=DEFAULT_TEST_PREP, question=question, correct=correct, subject=subject,
        selected_choice=selected_choice,
    )
    stats = get_practice_stats(user, subject)
    stats.answered = F('answered') + 1
    if correct:
        stats.correct = F('correct') + 1
    stats.save(update_fields=['answered', 'correct'])

    # Question-bank progress counts DISTINCT questions, so only the first
    # attempt at a question moves the per-type counters (same rule as Elo).
    if first_attempt and question.question_type:
        type_stats, _ = PracticeTypeStats.objects.get_or_create(
            user=user, test_prep_id=DEFAULT_TEST_PREP, subject=subject,
            question_type=question.question_type,
        )
        type_stats.solved = F('solved') + 1
        fields = ['solved']
        if correct:
            type_stats.correct = F('correct') + 1
            fields.append('correct')
        type_stats.save(update_fields=fields)


def practice_type_progress(user, subjects=None):
    """Question-bank progress per type: solved (distinct attempted) vs total.

    Returns {subject: [{'type', 'solved', 'correct', 'total'}, ...]} in the
    taxonomy's teaching order. Totals come from the live Question table, so
    solved is clamped in case questions were deleted after being attempted.
    """
    subjects = [s for s in (subjects or SUBJECTS) if s in SUBJECT_TYPES]
    types = [t for s in subjects for t in SUBJECT_TYPES[s]]
    totals = {
        row['question_type']: row['total']
        for row in Question.objects.filter(test_prep_id=DEFAULT_TEST_PREP, question_type__in=types)
        .values('question_type').annotate(total=Count('id'))
    }
    stats = {
        row.question_type: row
        for row in PracticeTypeStats.objects.filter(
            user=user, test_prep_id=DEFAULT_TEST_PREP, question_type__in=types,
        )
    }
    payload = {}
    for subject in subjects:
        entries = []
        for question_type in SUBJECT_TYPES[subject]:
            total = totals.get(question_type, 0)
            row = stats.get(question_type)
            solved = min(row.solved, total) if row else 0
            correct = min(row.correct, solved) if row else 0
            entries.append({
                'type': question_type,
                'solved': solved,
                'correct': correct,
                'total': total,
            })
        payload[subject] = entries
    return payload


def practice_current_streak(user):
    streak = 0
    for correct in PracticeAttempt.objects.filter(
        user=user, test_prep_id=DEFAULT_TEST_PREP,
    ).order_by('-created_at', '-id').values_list('correct', flat=True):
        if not correct:
            break
        streak += 1
    return streak

USER_K_PROVISIONAL = 32
USER_K_STABLE = 16
QUESTION_K = 8
PROVISIONAL_ATTEMPTS = 30

RATING_MIN, RATING_MAX = 100, 3000


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------

def get_quota(user):
    """Return (used_today, limit, has_premium). limit is None for premium."""
    profile = getattr(user, 'profile', None)
    has_premium = bool(profile and profile.has_premium)
    start, end = _local_day_bounds_utc(profile, _local_today(profile))
    used = PracticeAttempt.objects.filter(
        user=user, test_prep_id=DEFAULT_TEST_PREP, created_at__gte=start, created_at__lt=end,
    ).count()
    limit = None if has_premium else settings.FREE_DAILY_LIMIT
    return used, limit, has_premium


def quota_payload(user):
    used, limit, has_premium = get_quota(user)
    profile = getattr(user, 'profile', None)
    _, reset_at = _local_day_bounds_utc(profile, _local_today(profile))
    return {
        'used': used,
        'limit': limit,
        'remaining': None if limit is None else max(0, limit - used),
        'is_premium': has_premium,
        'reset_at': reset_at.isoformat(),
        'timezone': str(_user_tz(profile)),
    }


# ---------------------------------------------------------------------------
# Elo (Elo-Davidson expected score, per-side K)
# ---------------------------------------------------------------------------

def _expected_score(rating_a, rating_b, kappa=1, s=400):
    r = rating_a - rating_b
    exponent = 10 ** (r / s)
    return (exponent + kappa / 2) / (10 ** (-r / s) + kappa + exponent)


def _clamp(rating):
    return max(RATING_MIN, min(RATING_MAX, int(rating)))


def apply_practice_elo(user, question, correct):
    """First-attempt-only rating update between a user and a question."""
    subject = subject_of(question)
    stats = get_practice_stats(user, subject)
    previous_rating = stats.elo

    already_attempted = PracticeAttempt.objects.filter(
        user=user, test_prep_id=DEFAULT_TEST_PREP, question=question,
    ).exists()
    if already_attempted:
        return {
            'rated': False, 'subject': subject,
            'previous_rating': previous_rating, 'new_rating': previous_rating, 'delta': 0,
        }

    total_attempts = PracticeAttempt.objects.filter(
        user=user, test_prep_id=DEFAULT_TEST_PREP,
    ).filter(subject_filter(subject)).count()
    user_k = USER_K_PROVISIONAL if total_attempts < PROVISIONAL_ATTEMPTS else USER_K_STABLE

    expected = _expected_score(previous_rating, question.sp_elo_rating)
    result = 1.0 if correct else 0.0

    new_rating = _clamp(previous_rating + user_k * (result - expected))
    stats.elo = new_rating
    stats.save(update_fields=['elo'])

    question.sp_elo_rating = _clamp(question.sp_elo_rating - QUESTION_K * (result - expected))
    question.save(update_fields=['sp_elo_rating'])
    return {
        'rated': True, 'subject': subject,
        'previous_rating': previous_rating, 'new_rating': new_rating,
        'delta': new_rating - previous_rating,
    }


# ---------------------------------------------------------------------------
# Practice filters (premium filter bar)
# ---------------------------------------------------------------------------

SAVED_MODES = ('all', 'only', 'exclude')          # any / saved only / unsaved only
ATTEMPTED_MODES = ('all', 'only', 'exclude')      # any / done before / new only
RESULT_MODES = ('all', 'correct', 'incorrect')    # any / got right / got wrong


def parse_practice_filters(request, subject):
    """Read the filter-bar query params into a normalized dict.

    Every filter defaults to its widest setting so an absent or empty param
    means "no filter". `types` and `levels` are comma-separated multi-selects;
    the legacy single `type` param is still honored for back-compat.
    """
    types_param = request.GET.get('types')
    if types_param is None:
        legacy = request.GET.get('type')
        types_param = legacy if legacy and legacy != 'any' else ''
    valid_types = set(SUBJECT_TYPES[subject])
    types = list(dict.fromkeys(t for t in types_param.split(',') if t in valid_types))

    levels = []
    for chunk in (request.GET.get('levels') or '').split(','):
        chunk = chunk.strip()
        if chunk.isdigit() and 1 <= int(chunk) <= 5:
            levels.append(int(chunk))

    def _one_of(name, allowed):
        value = request.GET.get(name, 'all')
        return value if value in allowed else 'all'

    return {
        'types': types,
        'levels': sorted(set(levels)),
        'saved': _one_of('saved', SAVED_MODES),
        'attempted': _one_of('attempted', ATTEMPTED_MODES),
        'result': _one_of('result', RESULT_MODES),
    }


def filters_are_default(filters):
    """True when nothing is narrowed — i.e. plain adaptive practice."""
    return (
        not filters['types'] and not filters['levels']
        and filters['saved'] == 'all'
        and filters['attempted'] == 'all'
        and filters['result'] == 'all'
    )


def filters_want_review(filters):
    """Whether the filters ask for already-answered questions. These open the
    review pool, which is exempt from the daily quota and never moves Elo."""
    return filters['result'] != 'all' or filters['attempted'] == 'only'


def topic_lane_signature(question_type):
    """Stable active-question lane for one topic (or the unfiltered pool)."""
    if not question_type:
        return 'any'
    return 't:' + hashlib.sha1(question_type.encode()).hexdigest()[:16]


def latest_attempt_correct_ids(user, correct):
    """Question ids whose user's MOST RECENT recorded attempt matches `correct`.

    Only counted (first-time) answers are recorded, so this reflects how a
    question was originally graded; impact-free reviews never change it.
    """
    latest = {}
    for question_id, was_correct in (
        PracticeAttempt.objects.filter(user=user, test_prep_id=DEFAULT_TEST_PREP)
        .order_by('created_at', 'id')
        .values_list('question_id', 'correct')
    ):
        latest[question_id] = was_correct  # later rows win → most recent attempt
    return {qid for qid, ok in latest.items() if ok == correct}


def pick_filtered_question(user, subject, filters):
    """Pick a practice question honoring the filter bar.

    Returns (question, empty_state). With default filters this is the adaptive
    fresh-only picker. The done-before/result filters instead draw from a
    review pool of already-answered questions.
    """
    base = Question.objects.filter(
        test_prep_id=DEFAULT_TEST_PREP, subject=subject,
        question_type__in=SUBJECT_TYPES[subject],
    )
    if filters['types']:
        base = base.filter(question_type__in=filters['types'])
    if filters['levels']:
        base = base.filter(difficulty__in=filters['levels'])

    if filters['saved'] != 'all':
        saved_ids = SavedQuestion.objects.filter(
            user=user, test_prep_id=DEFAULT_TEST_PREP,
        ).values_list('question_id', flat=True)
        if filters['saved'] == 'only':
            base = base.filter(id__in=saved_ids)
        else:  # 'exclude'
            base = base.exclude(id__in=saved_ids)

    if not base.exists():
        return None, 'no_questions'

    attempted_ids = set(
        PracticeAttempt.objects.filter(
            user=user, test_prep_id=DEFAULT_TEST_PREP,
        ).values_list('question_id', flat=True)
    )

    if filters_want_review(filters):
        pool = base.filter(id__in=attempted_ids)
        if filters['result'] == 'correct':
            pool = pool.filter(id__in=latest_attempt_correct_ids(user, True))
        elif filters['result'] == 'incorrect':
            pool = pool.filter(id__in=latest_attempt_correct_ids(user, False))
        ids = list(pool.values_list('id', flat=True))
        if not ids:
            return None, 'no_matches'
        return Question.objects.get(id=random.choice(ids)), None

    # Fresh pool — practice never recycles a question the student has answered.
    fresh = base.exclude(id__in=attempted_ids)
    if not fresh.exists():
        return None, 'completed_topic'

    user_rating = get_practice_stats(user, subject).elo
    for window in (250, 500, 1000, None):
        qs = fresh
        if window is not None:
            qs = qs.filter(
                sp_elo_rating__gte=user_rating - window,
                sp_elo_rating__lte=user_rating + window,
            )
        fresh_ids = list(qs.values_list('id', flat=True))
        if fresh_ids:
            return Question.objects.get(id=random.choice(fresh_ids)), None

    return None, 'completed_topic'


# ---------------------------------------------------------------------------
# Question selection
# ---------------------------------------------------------------------------

def pick_practice_question(user, subject, question_type=None):
    """Pick an unattempted question near the user's rating.

    The caller distinguishes an empty topic from a completed one so practice
    never silently recycles a question the student already answered.
    """
    types = SUBJECT_TYPES[subject]
    user_rating = get_practice_stats(user, subject).elo

    base = Question.objects.filter(test_prep_id=DEFAULT_TEST_PREP, subject=subject)
    if question_type and question_type != 'any' and question_type in types:
        base = base.filter(question_type=question_type)
    else:
        base = base.filter(question_type__in=types)

    if not base.exists():
        return None, 'no_questions'

    attempted_ids = set(
        PracticeAttempt.objects.filter(
            user=user, test_prep_id=DEFAULT_TEST_PREP,
        ).values_list('question_id', flat=True)
    )
    fresh = base.exclude(id__in=attempted_ids)
    if not fresh.exists():
        return None, 'completed_topic'

    # Widen the rating window until we find fresh questions.
    for window in (250, 500, 1000, None):
        qs = fresh
        if window is not None:
            qs = qs.filter(
                sp_elo_rating__gte=user_rating - window,
                sp_elo_rating__lte=user_rating + window,
            )
        fresh_ids = list(qs.values_list('id', flat=True))
        if fresh_ids:
            return Question.objects.get(id=random.choice(fresh_ids)), None

    return None, 'completed_topic'


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@api_view(['GET'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def next_question(request):
    """Serve the next practice question, enforcing tier rules and filters."""
    subject = resolve_subject(request.GET.get('subject'))
    quota = quota_payload(request.user)
    filters = parse_practice_filters(request, subject)

    if not filters_are_default(filters) and not quota['is_premium']:
        return Response(
            {'error': 'premium_required',
             'detail': 'Practice filters are a premium feature. Free practice serves random questions.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    # The quota only guards fresh questions; reviewing answered ones is free.
    review = filters_want_review(filters)
    if not review and quota['remaining'] is not None and quota['remaining'] <= 0:
        return Response(
            {'error': 'daily_limit', 'quota': quota,
             'detail': 'You have used your free questions for today. Upgrade for unlimited practice.'},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    empty_states = []
    for question_type in filters['types'] or [None]:
        lane = f'{subject}:{topic_lane_signature(question_type)}'
        active = PracticeActiveQuestion.objects.filter(
            user=request.user, test_prep_id=DEFAULT_TEST_PREP, lane=lane,
        ).first()
        if active:
            return Response({
                'question': QuestionSerializer(active.question).data,
                'quota': quota, 'subject': subject,
            })

        lane_filters = {**filters, 'types': [question_type] if question_type else []}
        question, empty_state = pick_filtered_question(request.user, subject, lane_filters)
        if question is None:
            empty_states.append(empty_state)
            continue

        active, _ = PracticeActiveQuestion.objects.get_or_create(
            user=request.user,
            test_prep_id=DEFAULT_TEST_PREP,
            lane=lane,
            defaults={'question': question},
        )
        return Response({
            'question': QuestionSerializer(active.question).data,
            'quota': quota, 'subject': subject,
        })

    empty_state = 'no_matches' if 'no_matches' in empty_states else (
        'completed_topic' if 'completed_topic' in empty_states else 'no_questions'
    )
    if empty_state == 'completed_topic':
        detail = f'You answered every matching {subject} practice question.'
    elif empty_state == 'no_matches':
        detail = 'No practice questions match these filters yet. Try widening them.'
    else:
        detail = f'No {subject} practice questions are available yet.'
    return Response({
        'error': empty_state,
        'detail': detail,
        'subject': subject,
        'quota': quota,
    }, status=status.HTTP_404_NOT_FOUND)


@api_view(['GET'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def practice_status(request):
    """Quota + tier + rating + day-streak snapshot for practice surfaces."""
    stats = practice_stats_breakdown(request.user)
    return Response({
        'quota': quota_payload(request.user),
        'sp_elo_rating': stats['english_elo'],
        'math_elo_rating': stats['math_elo'],
        'stats': stats,
        'topics': SUBJECT_TYPES,
        'type_progress': practice_type_progress(request.user),
        'daily': daily_snapshot(request.user),
    })


def exclude_live_questions(queryset, user):
    """Drop rows whose question is live for `user` in a duel or tournament.

    Review surfaces hand back the correct answer and explanation, which would
    be a cheat sheet for a question the user still has to answer for real.
    """
    from api.models import TournamentParticipation, TrackedQuestion
    tournament_questions = TournamentParticipation.live_for(user).values_list(
        'tournament__questions', flat=True,
    )
    duel_questions = TrackedQuestion.objects.filter(
        user=user, room__status='Battling', status='Blank',
    ).values_list('question_id', flat=True)
    return queryset.exclude(
        question_id__in=tournament_questions,
    ).exclude(
        question_id__in=duel_questions,
    )


def review_question_payload(question):
    """Question shape used by the review surfaces (history and saved).

    Unlike QuestionSerializer this deliberately includes the answer and
    explanation — callers must gate it through exclude_live_questions().
    """
    return {
        'id': question.id,
        'question': question.question,
        'choices': [question.choice_a, question.choice_b, question.choice_c, question.choice_d],
        'difficulty': question.difficulty,
        'question_type': question.question_type,
        'correct_choice_label': question.answer,
        'correct_answer': question.answer_text,
        'explanation': question.explanation,
    }


@api_view(['GET'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def practice_history(request):
    """Return the user's past practice answers, newest first."""
    subject = request.GET.get('subject')
    if subject not in SUBJECTS:
        subject = None
    question_type = request.GET.get('question_type')
    if question_type == 'all':
        question_type = None
    incorrect_only = request.GET.get('incorrect_only', '').lower() == 'true'
    try:
        offset = max(0, int(request.GET.get('offset', 0)))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = min(50, max(1, int(request.GET.get('limit', 20))))
    except (TypeError, ValueError):
        limit = 20

    attempts = exclude_live_questions(
        PracticeAttempt.objects.filter(user=request.user, test_prep_id=DEFAULT_TEST_PREP), request.user,
    )
    if subject:
        attempts = attempts.filter(subject=subject)

    question_types = list(attempts.filter(
        question__question_type__isnull=False,
    ).exclude(
        question__question_type='',
    ).order_by('question__question_type').values_list(
        'question__question_type', flat=True,
    ).distinct())
    if question_type:
        attempts = attempts.filter(question__question_type=question_type)

    mistake_count = attempts.filter(correct=False).values(
        'question_id',
    ).distinct().count()
    if incorrect_only:
        attempts = attempts.filter(correct=False)

    attempts = attempts.select_related('question').order_by('-created_at', '-id')
    page = list(attempts[offset:offset + limit + 1])
    has_more = len(page) > limit

    return Response({
        'attempts': [{
            'id': attempt.id,
            'subject': attempt.subject,
            'correct': attempt.correct,
            'selected_choice': attempt.selected_choice,
            'created_at': attempt.created_at.isoformat(),
            'question': review_question_payload(attempt.question),
        } for attempt in page[:limit]],
        'question_types': question_types,
        'mistake_count': mistake_count,
        'has_more': has_more,
        'next_offset': offset + limit if has_more else None,
    })


# ---------------------------------------------------------------------------
# Saved questions ("mark for review")
# ---------------------------------------------------------------------------

@api_view(['GET', 'POST'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def saved_questions(request):
    """GET the user's saved questions (newest first); POST to save one."""
    if request.method == 'POST':
        question_id = request.data.get('question_id')
        question = Question.objects.filter(id=question_id, test_prep_id=DEFAULT_TEST_PREP).first()
        if question is None:
            return Response({'error': 'not_found', 'detail': 'That question does not exist.'},
                            status=status.HTTP_404_NOT_FOUND)
        # Idempotent: re-saving an already-saved question is a no-op rather
        # than an error, so a double click can't fail the button.
        SavedQuestion.objects.get_or_create(
            user=request.user,
            test_prep_id=DEFAULT_TEST_PREP,
            question=question,
            defaults={'subject': resolve_subject(subject_of(question))},
        )
        return Response({'saved': True, 'question_id': question.id})

    subject = request.GET.get('subject')
    if subject not in SUBJECTS:
        subject = None
    question_type = request.GET.get('question_type')
    if question_type == 'all':
        question_type = None
    try:
        offset = max(0, int(request.GET.get('offset', 0)))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = min(50, max(1, int(request.GET.get('limit', 20))))
    except (TypeError, ValueError):
        limit = 20

    saved = exclude_live_questions(
        SavedQuestion.objects.filter(user=request.user, test_prep_id=DEFAULT_TEST_PREP), request.user,
    )
    if subject:
        saved = saved.filter(subject=subject)

    question_types = list(saved.filter(
        question__question_type__isnull=False,
    ).exclude(
        question__question_type='',
    ).order_by('question__question_type').values_list(
        'question__question_type', flat=True,
    ).distinct())
    if question_type:
        saved = saved.filter(question__question_type=question_type)

    saved_count = saved.count()
    saved = saved.select_related('question').order_by('-created_at', '-id')
    page = list(saved[offset:offset + limit + 1])
    has_more = len(page) > limit

    return Response({
        'saved': [{
            'id': row.id,
            'subject': row.subject,
            'created_at': row.created_at.isoformat(),
            'question': review_question_payload(row.question),
        } for row in page[:limit]],
        'question_types': question_types,
        'saved_count': saved_count,
        'has_more': has_more,
        'next_offset': offset + limit if has_more else None,
    })


@api_view(['DELETE'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def unsave_question(request, question_id):
    """Remove a question from the user's saved list."""
    SavedQuestion.objects.filter(
        user=request.user, test_prep_id=DEFAULT_TEST_PREP, question_id=question_id,
    ).delete()
    return Response({'saved': False, 'question_id': question_id})


@api_view(['GET'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def saved_question_status(request):
    """Whether a single question is saved — drives the mark-for-review button."""
    try:
        question_id = int(request.GET.get('question_id'))
    except (TypeError, ValueError):
        return Response({'error': 'question_id is required.'}, status=status.HTTP_400_BAD_REQUEST)
    return Response({
        'saved': SavedQuestion.objects.filter(
            user=request.user, test_prep_id=DEFAULT_TEST_PREP, question_id=question_id,
        ).exists(),
        'question_id': question_id,
    })


# ---------------------------------------------------------------------------
# Day streak (practice-completion based)
#
# Completing DAILY_PRACTICE_GOAL practice answers within the user's LOCAL day
# extends the streak. Updated at answer-time and evaluated lazily at read-time,
# so no midnight cron job is needed: a missed day simply reads as a streak of 0.
# ---------------------------------------------------------------------------
import datetime

import pytz


def _user_tz(profile):
    try:
        return pytz.timezone(profile.timezone) if profile else pytz.UTC
    except Exception:
        return pytz.UTC


def _local_today(profile):
    return timezone.now().astimezone(_user_tz(profile)).date()


def _local_day_bounds_utc(profile, local_date):
    """UTC datetimes spanning the given local calendar day."""
    tz = _user_tz(profile)
    start = tz.localize(datetime.datetime.combine(local_date, datetime.time.min))
    end = tz.localize(datetime.datetime.combine(
        local_date + datetime.timedelta(days=1), datetime.time.min,
    ))
    return start.astimezone(pytz.UTC), end.astimezone(pytz.UTC)


def _attempts_on(user, profile, local_date):
    start, end = _local_day_bounds_utc(profile, local_date)
    return PracticeAttempt.objects.filter(
        user=user, test_prep_id=DEFAULT_TEST_PREP, created_at__gte=start, created_at__lt=end,
    ).count()


def effective_streak(profile, today=None):
    """Stored streak counts only if the last completion was today or yesterday."""
    if profile is None or profile.last_practice_completed is None:
        return 0
    today = today or _local_today(profile)
    if profile.last_practice_completed >= today - datetime.timedelta(days=1):
        return profile.practice_streak
    return 0


def update_daily_streak(user):
    """Called after each practice attempt. Extends the streak the moment the
    user's answer count for their local day reaches the goal."""
    profile = getattr(user, 'profile', None)
    goal = settings.DAILY_PRACTICE_GOAL
    if profile is None:
        return {'count': 0, 'goal': goal, 'completed_today': False,
                'streak': 0, 'longest': 0, 'streak_extended': False}

    today = _local_today(profile)
    count = _attempts_on(user, profile, today)
    _, day_ends_at = _local_day_bounds_utc(profile, today)
    completed = count >= goal
    extended = False

    if completed and profile.last_practice_completed != today:
        yesterday = today - datetime.timedelta(days=1)
        if profile.last_practice_completed == yesterday:
            profile.practice_streak += 1
        else:
            profile.practice_streak = 1
        profile.longest_practice_streak = max(
            profile.longest_practice_streak, profile.practice_streak,
        )
        profile.last_practice_completed = today
        profile.save(update_fields=[
            'practice_streak', 'longest_practice_streak', 'last_practice_completed',
        ])
        extended = True

    return {
        'count': count,
        'goal': goal,
        'completed_today': profile.last_practice_completed == today,
        'streak': effective_streak(profile, today),
        'longest': profile.longest_practice_streak,
        'streak_extended': extended,
        'day_ends_at': day_ends_at.isoformat(),
        'timezone': str(_user_tz(profile)),
    }


def week_strip(user, profile):
    """Last 7 local days, oldest first: which days hit the goal."""
    today = _local_today(profile)
    goal = settings.DAILY_PRACTICE_GOAL
    start_date = today - datetime.timedelta(days=6)
    start_utc, _ = _local_day_bounds_utc(profile, start_date)

    tz = _user_tz(profile)
    counts = {}
    stamps = PracticeAttempt.objects.filter(
        user=user, test_prep_id=DEFAULT_TEST_PREP, created_at__gte=start_utc,
    ).values_list('created_at', flat=True)
    for stamp in stamps:
        local_date = stamp.astimezone(tz).date()
        counts[local_date] = counts.get(local_date, 0) + 1

    days = []
    for offset in range(7):
        d = start_date + datetime.timedelta(days=offset)
        days.append({
            'date': d.isoformat(),
            'weekday': d.strftime('%a'),
            'count': counts.get(d, 0),
            'completed': counts.get(d, 0) >= goal,
            'is_today': d == today,
        })
    return days


def practice_activity(user, days=365):
    """Daily answered-question totals for the profile activity grid."""
    profile = getattr(user, 'profile', None)
    today = _local_today(profile)
    start_date = today - datetime.timedelta(days=days - 1)
    start_utc, _ = _local_day_bounds_utc(profile, start_date)
    _, end_utc = _local_day_bounds_utc(profile, today)
    tz = _user_tz(profile)
    counts = {}
    attempts = PracticeAttempt.objects.filter(
        user=user, test_prep_id=DEFAULT_TEST_PREP,
        created_at__gte=start_utc, created_at__lt=end_utc,
    ).values_list('created_at', 'subject')
    for stamp, subject in attempts:
        local_date = stamp.astimezone(tz).date()
        day = counts.setdefault(local_date, {'count': 0, 'english': 0, 'math': 0})
        day['count'] += 1
        day[subject] += 1

    return [
        {'date': date.isoformat(), **counts.get(date, {'count': 0, 'english': 0, 'math': 0})}
        for date in (start_date + datetime.timedelta(days=offset) for offset in range(days))
    ]


def daily_snapshot(user):
    """Full streak block for dashboards: today's progress + week strip."""
    profile = getattr(user, 'profile', None)
    goal = settings.DAILY_PRACTICE_GOAL
    if profile is None:
        return {'count': 0, 'goal': goal, 'completed_today': False,
                'streak': 0, 'longest': 0, 'week': []}
    today = _local_today(profile)
    count = _attempts_on(user, profile, today)
    _, day_ends_at = _local_day_bounds_utc(profile, today)
    return {
        'count': count,
        'goal': goal,
        'completed_today': profile.last_practice_completed == today,
        'streak': effective_streak(profile, today),
        'longest': profile.longest_practice_streak,
        'week': week_strip(user, profile),
        'day_ends_at': day_ends_at.isoformat(),
        'timezone': str(_user_tz(profile)),
    }


# =========================================================
# Full-length practice tests (saved results + history)
# =========================================================

def _test_result_payload(result):
    return {
        'id': result.id,
        'test_id': result.test_id,
        'test_name': result.test_name,
        'score': result.score,
        'correct': result.correct,
        'total': result.total,
        'time_used_seconds': result.time_used_seconds,
        'questions': result.questions,
        'created_at': result.created_at.isoformat(),
    }


@api_view(['POST'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def save_test_result(request):
    """Persist a finished practice test.

    Returns the user's previous best/last scores (from BEFORE this save) so
    the result page can show "vs last test" and "new personal best" feedback
    without a second round-trip.
    """
    data = request.data
    try:
        score = int(data['score'])
        correct = int(data['correct'])
        total = int(data['total'])
    except (KeyError, TypeError, ValueError):
        return Response(
            {'error': 'score, correct and total are required integers'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    questions = data.get('questions')
    if not isinstance(questions, list):
        questions = []
    try:
        time_used = int(data['time_used_seconds'])
    except (KeyError, TypeError, ValueError):
        time_used = None

    previous = PracticeTestResult.objects.filter(user=request.user, test_prep_id=DEFAULT_TEST_PREP)
    previous_best = previous.aggregate(best=Max('score'))['best']
    previous_last = previous.values_list('score', flat=True).first()

    result = PracticeTestResult.objects.create(
        user=request.user,
        test_prep_id=DEFAULT_TEST_PREP,
        test_id=int(data.get('test_id') or 1),
        test_name=str(data.get('test_name') or 'Practice Test')[:100],
        score=score,
        correct=correct,
        total=total,
        time_used_seconds=time_used,
        questions=[
            {
                'question_id': q.get('question_id'),
                'user_choice': q.get('user_choice'),
                'correct': bool(q.get('correct')),
            }
            for q in questions if isinstance(q, dict)
        ],
    )
    return Response({
        'result': _test_result_payload(result),
        'previous_best': previous_best,
        'previous_last': previous_last,
    })


@api_view(['GET'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def test_history(request):
    """All of the user's saved practice tests, newest first, plus summary stats."""
    # ponytail: returns every row (tests are ~1/sitting); paginate if users
    # ever accumulate hundreds.
    rows = list(PracticeTestResult.objects.filter(
        user=request.user, test_prep_id=DEFAULT_TEST_PREP,
    ))
    scores = [row.score for row in rows]
    return Response({
        'results': [_test_result_payload(row) for row in rows],
        'tests_taken': len(rows),
        'best_score': max(scores) if scores else None,
        'average_score': round(sum(scores) / len(scores)) if scores else None,
        'latest_score': scores[0] if scores else None,
    })
