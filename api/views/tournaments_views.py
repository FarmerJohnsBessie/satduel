import random
from datetime import timedelta
from django.contrib.auth.models import User
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone
from django.shortcuts import get_object_or_404
import string

from api.models import Tournament, TournamentParticipation, Question, TournamentQuestion, Profile
from api.views.serializers import TournamentSerializer, TournamentParticipationSerializer, TournamentQuestionSerializer, \
    TPSubmitAnswerSerializer


# DRF has no DEFAULT_PERMISSION_CLASSES here, so an @api_view without an explicit
# @permission_classes is open to the anonymous internet. These two used to expose
# create/edit/delete that way — any passer-by could rewrite or delete a tournament
# and cascade away every participation in it. Nothing in the app called them
# (creation goes through create/ and admin_create/), so they are gone rather than
# guarded.
@api_view(['GET'])
def tournament_list(request):
    tournaments = Tournament.objects.filter(private=False, end_time__gt=timezone.now())
    serializer = TournamentSerializer(tournaments, many=True)
    return Response(serializer.data)


@api_view(['GET'])
def tournament_detail(request, pk):
    tournament = get_object_or_404(Tournament, pk=pk)
    return Response(TournamentSerializer(tournament).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def join_tournament(request, pk):
    tournament = get_object_or_404(Tournament, pk=pk)
    user = request.user
    now = timezone.now()

    participation = TournamentParticipation.objects.filter(user=user, tournament=tournament).first()
    if participation:
        # A run whose clock expired while the tab was closed reads as Completed
        # here, so the client sends the user to review instead of a dead round.
        serializer = TournamentParticipationSerializer(participation.expire_if_over())
        return Response(serializer.data, status=status.HTTP_200_OK)

    if now < tournament.start_time:
        return Response({"error": "This tournament has not started yet."}, status=status.HTTP_400_BAD_REQUEST)
    if tournament.end_time and now >= tournament.end_time:
        return Response({"error": "This tournament has already closed."}, status=status.HTTP_400_BAD_REQUEST)

    # Joining ten minutes before close buys ten minutes, not a full duration that
    # runs past the tournament's own deadline.
    end_time = now + tournament.duration
    if tournament.end_time:
        end_time = min(end_time, tournament.end_time)

    participation = TournamentParticipation.objects.create(
        user=user,
        tournament=tournament,
        start_time=now,
        end_time=end_time,
        status='Active'
    )

    TournamentQuestion.objects.bulk_create([
        TournamentQuestion(participation=participation, question=question, status='Blank')
        for question in tournament.questions.all()
    ])

    serializer = TournamentParticipationSerializer(participation)
    return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_participation(request, pk):
    user = request.user
    tournament = get_object_or_404(Tournament, pk=pk)
    participation = get_object_or_404(TournamentParticipation, user=user, tournament=tournament)
    serializer = TournamentParticipationSerializer(participation.expire_if_over())
    return Response(serializer.data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def get_tournament_questions(request, pk):
    user = request.user
    tournament = Tournament.objects.get(id=pk)
    participation = TournamentParticipation.objects.get(user=user, tournament=tournament)

    tournament_questions = TournamentQuestion.objects.filter(participation=participation).order_by('id')
    serializer = TournamentQuestionSerializer(tournament_questions, many=True)
    return Response(serializer.data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def tournament_leaderboard(request, pk):
    """Ranked runs for one tournament.

    The per-question correct/incorrect grid is the ghost-race display, but it is
    also a cheat sheet: post three different choices from three accounts and the
    statuses tell you the answer. It used to be readable by anyone, logged in or
    not. Now it needs a login, and the grid only goes to people who are actually
    in the round; everyone else gets names, scores and ranks.
    """
    tournament = get_object_or_404(Tournament, pk=pk)
    participations = (
        TournamentParticipation.objects
        .filter(tournament=tournament)
        .select_related('user')
        .prefetch_related('tournamentquestion_set')
        .order_by('-score', 'last_correct_submission')
    )
    in_round = TournamentParticipation.objects.filter(
        user=request.user, tournament=tournament,
    ).exists()
    serializer = TPSubmitAnswerSerializer(
        participations, many=True, context={'show_questions': in_round},
    )
    return Response(serializer.data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def submit_answer(request, pk):
    """Record one answer for the caller's own run.

    This used to trust the client completely: it looked up the answer row by a
    bare id, so any id would do, and it did `score += 1` on every correct POST.
    Re-posting one right answer six times therefore scored six points on a
    four-question round. Every guard below closes one of those holes, and the
    score is recounted from the rows so it can never drift from them again.
    """
    tournament = get_object_or_404(Tournament, pk=pk)
    participation = get_object_or_404(
        TournamentParticipation, user=request.user, tournament=tournament,
    ).expire_if_over()

    selected_choice = request.data.get('selected_choice')
    tournament_question_id = request.data.get('tournament_question_id')

    if not selected_choice or not tournament_question_id:
        return Response({"error": "Question and answer are required"}, status=status.HTTP_400_BAD_REQUEST)

    if participation.status != 'Active':
        return Response({"error": "Your round is over."}, status=status.HTTP_403_FORBIDDEN)

    # Scoping the lookup to the caller's own participation is what stops one
    # player writing into another player's answer row — and it also makes a
    # question from some other tournament a 404 instead of a scored answer.
    tournament_question = get_object_or_404(
        TournamentQuestion, id=tournament_question_id, participation=participation,
    )

    if tournament_question.status != 'Blank':
        return Response({"error": "You already answered this question."}, status=status.HTTP_409_CONFLICT)

    question = tournament_question.question
    is_correct = question.answer_text == selected_choice
    time_taken = timezone.now() - participation.start_time

    tournament_question.status = 'Correct' if is_correct else 'Incorrect'
    tournament_question.selected_choice = selected_choice
    tournament_question.time_taken = time_taken
    tournament_question.save(update_fields=['status', 'selected_choice', 'time_taken'])

    participation.score = TournamentQuestion.objects.filter(
        participation=participation, status='Correct',
    ).count()
    if is_correct:
        participation.last_correct_submission = time_taken
    participation.save(update_fields=['score', 'last_correct_submission'])

    serializer = TournamentQuestionSerializer(tournament_question)
    return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def finish_participation(request, pk):
    tournament = get_object_or_404(Tournament, pk=pk)
    user = request.user
    participation = get_object_or_404(TournamentParticipation, user=user, tournament=tournament)
    if participation.status == 'Active':
        # Finishing early stops the clock; the run's own deadline is what review
        # and the leaderboard compare against, so record when it actually ended.
        participation.end_time = min(participation.end_time or timezone.now(), timezone.now())
        participation.status = 'Completed'
        participation.save(update_fields=['status', 'end_time'])
    serializer = TournamentParticipationSerializer(participation)
    return Response(serializer.data)


def generate_unique_join_code():
    """Generate a unique 6-character alphanumeric join code."""
    code_length = 6
    characters = string.ascii_uppercase + string.digits
    while True:
        join_code = ''.join(random.choices(characters, k=code_length))
        if not Tournament.objects.filter(join_code=join_code).exists():
            return join_code


def parse_duration(value):
    if isinstance(value, str) and ':' in value:
        hours, minutes, seconds = [int(part) for part in value.split(':')]
        return timedelta(hours=hours, minutes=minutes, seconds=seconds)
    return timedelta(minutes=int(value or 30))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def create_tournament(request):
    data = request.data
    questions_data = data['questions']

    join_code = generate_unique_join_code() if data['private'] else None

    tournament = Tournament.objects.create(
        name=data['name'],
        description=data['description'],
        start_time=data['start_time'],
        end_time=data['end_time'],
        duration=parse_duration(data.get('duration')),
        private=data['private'],
        join_code=join_code,
    )

    for question in questions_data:
        question = Question.objects.create(
            question=question['question'],
            choice_a=question['choice_a'],
            choice_b=question['choice_b'],
            choice_c=question['choice_c'],
            choice_d=question['choice_d'],
            answer=question['answer'],
            difficulty=question['difficulty'],
            question_type=question.get('question_type', ''),  # Default empty string if not provided
            explanation=question.get('explanation', ''),  # Default empty string if not provided
        )
        tournament.questions.add(question)
    tournament.save()
    # create() was handed raw strings from the request body, so the in-memory
    # instance still holds strings where the model declares datetimes.
    tournament.refresh_from_db()
    request.user.profile.my_tournaments.add(tournament)
    serializer = TournamentSerializer(tournament)
    return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def create_tournament_admin(request):
    data = request.data
    join_code = generate_unique_join_code() if data.get('private', False) else None

    # Extract and validate the data
    question_ids = data['question_ids']
    tournament = Tournament.objects.create(
        name=data['name'],
        description=data['description'],
        start_time=data['start_time'],
        end_time=data['end_time'],
        private=data.get('private', False),
        duration=parse_duration(data.get('duration')),
        join_code=join_code,
    )

    # Associate the selected questions with the tournament
    questions = Question.objects.filter(id__in=question_ids)
    tournament.questions.set(questions)
    tournament.save()
    tournament.refresh_from_db()
    profile = Profile.objects.get(user=request.user)
    profile.my_tournaments.add(tournament)
    serializer = TournamentSerializer(tournament)
    return Response(serializer.data, status=status.HTTP_201_CREATED)

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def tournament_history(request, id=None):
    """Paginated, N+1-free tournament history.

    The old version serialized every participation with a nested
    TournamentSerializer whose participantNumber/questionNumber properties
    each ran an extra COUNT query per row — 2N+1 queries total. This fetches
    everything in one annotated queryset and paginates.
    """
    from django.db.models import Count

    user = User.objects.get(id=id) if id else request.user

    try:
        page = max(1, int(request.GET.get('page', 1)))
        page_size = min(50, max(1, int(request.GET.get('page_size', 10))))
    except ValueError:
        page, page_size = 1, 10

    participations = (
        TournamentParticipation.objects
        .filter(user=user, status="Completed")
        .select_related('tournament')
        .annotate(
            participant_count=Count('tournament__tournamentparticipation', distinct=True),
            question_count=Count('tournament__questions', distinct=True),
        )
        .order_by('-start_time')
    )
    total = participations.count()
    offset = (page - 1) * page_size

    results = [
        {
            'id': p.id,
            'user': p.user_id,
            'tournament': {
                'id': p.tournament.id,
                'name': p.tournament.name,
                'description': p.tournament.description,
                'duration': str(p.tournament.duration),
                'start_time': p.tournament.start_time,
                'end_time': p.tournament.end_time,
                'participantNumber': p.participant_count,
                'questionNumber': p.question_count,
                'private': p.tournament.private,
                'join_code': p.tournament.join_code,
            },
            'start_time': p.start_time,
            'end_time': p.end_time,
            'score': p.score,
            'last_correct_submission': p.last_correct_submission,
            'status': p.status,
        }
        for p in participations[offset:offset + page_size]
    ]

    # Plain list responses stay backward-compatible with existing consumers;
    # pagination metadata rides along in headers.
    response = Response(results)
    response['X-Total-Count'] = str(total)
    response['X-Page'] = str(page)
    response['X-Page-Size'] = str(page_size)
    return response


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def join_from_code(request):
    data = request.data
    join_code = data.get('join_code', '').strip()
    try:
        tournament = Tournament.objects.get(join_code=join_code)
    except Tournament.DoesNotExist:
        return Response({"error": "Invalid join code. Please try again."}, status=status.HTTP_400_BAD_REQUEST)

    return Response({"id": tournament.id}, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_my_tournaments(request):
    user = request.user
    profile = user.profile  # Assuming you have a one-to-one relationship
    tournaments = profile.my_tournaments.all()
    serializer = TournamentSerializer(tournaments, many=True)
    return Response(serializer.data)
