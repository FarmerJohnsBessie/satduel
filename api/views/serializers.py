from django.utils import timezone
from django.contrib.auth.models import User
from api.models import DUEL_EMOJIS, PREMIUM_DUEL_EMOJIS, DirectMessage, Question, Profile, Room, TrackedQuestion, FriendRequest, \
    UserStatistics, Tournament, TournamentParticipation, TournamentQuestion
from rest_framework import serializers


class QuestionSerializer(serializers.ModelSerializer):
    """Public question payload — must never include the correct answer or explanation."""
    choices = serializers.SerializerMethodField()
    source_display = serializers.CharField(source='get_source_display', read_only=True)
    test_prep = serializers.CharField(source='test_prep_id', read_only=True)

    class Meta:
        model = Question
        fields = [
            'id', 'question', 'choices', 'difficulty', 'question_type', 'test_prep', 'subject',
            'source', 'source_display', 'source_other',
            'choice_a', 'choice_b', 'choice_c', 'choice_d',
        ]

    def get_choices(self, obj):
        return [obj.choice_a, obj.choice_b, obj.choice_c, obj.choice_d]

class QuestionAdminSerializer(QuestionSerializer):
    """Full question payload including answer/explanation — staff only."""

    class Meta(QuestionSerializer.Meta):
        fields = QuestionSerializer.Meta.fields + ['answer', 'explanation']


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'first_name', 'last_name', 'email']
        read_only_fields = ['id', 'username', 'email']


class DuelUserSerializer(serializers.ModelSerializer):
    avatar = serializers.CharField(source='profile.avatar', read_only=True)
    avatar_icon = serializers.CharField(source='profile.avatar_icon', read_only=True)
    elo_rating = serializers.IntegerField(source='profile.elo_rating', read_only=True)
    duel_emotes = serializers.JSONField(source='profile.duel_emotes', read_only=True)
    is_premium = serializers.SerializerMethodField()

    def get_is_premium(self, obj):
        return obj.profile.has_premium

    class Meta:
        model = User
        fields = ['id', 'username', 'avatar', 'avatar_icon', 'elo_rating', 'duel_emotes', 'is_premium']


class ProfileSerializer(serializers.ModelSerializer):
    user = UserSerializer()
    active_test_prep = serializers.CharField(source='active_test_prep_id', read_only=True)
    is_premium = serializers.SerializerMethodField()
    # Practice ratings live on PracticeStats; keep the old payload keys.
    sp_elo_rating = serializers.SerializerMethodField()
    math_elo_rating = serializers.SerializerMethodField()
    duel_emotes = serializers.ListField(
        child=serializers.ChoiceField(choices=DUEL_EMOJIS),
        min_length=4,
        max_length=4,
    )

    class Meta:
        model = Profile
        fields = ['id', 'user', 'biography', 'grade', 'country', 'active_test_prep', 'avatar', 'avatar_icon', 'elo_rating', 'sp_elo_rating',
                  'math_elo_rating', 'is_premium', 'max_streak', 'duel_emotes']

    def get_is_premium(self, obj):
        return obj.has_premium

    def _subject_elo(self, obj, subject):
        for stats in obj.user.practice_stats.all():
            if stats.test_prep_id == 'sat' and stats.subject == subject:
                return stats.elo
        return 1200

    def get_sp_elo_rating(self, obj):
        return self._subject_elo(obj, 'english')

    def get_math_elo_rating(self, obj):
        return self._subject_elo(obj, 'math')

    def validate_duel_emotes(self, value):
        if len(set(value)) != 4:
            raise serializers.ValidationError('Choose four different duel emotes.')
        if any(emoji in PREMIUM_DUEL_EMOJIS for emoji in value) and not self.instance.has_premium:
            raise serializers.ValidationError('Premium is required for those emotes.')
        return value

    def update(self, instance, validated_data):
        user_data = validated_data.pop('user', {})
        user = instance.user

        if user_data:
            for attr in ('first_name', 'last_name'):
                if attr in user_data:
                    setattr(user, attr, user_data[attr])
            user.save(update_fields=['first_name', 'last_name'])

        # Update profile fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        
        return instance


class ProfileBiographySerializer(serializers.ModelSerializer):
    class Meta:
        model = Profile
        fields = ['id', 'biography']


class InfiniteQuestionsSerializer(serializers.ModelSerializer):
    user = UserSerializer()
    total_multiplier = serializers.SerializerMethodField()

    class Meta:
        model = UserStatistics
        fields = ['id', 'user', 'coins', 'total_multiplier']

    def get_total_multiplier(self, obj):
        return obj.total_multiplier()


class RoomSerializer(serializers.ModelSerializer):
    user1 = serializers.SerializerMethodField()
    user2 = serializers.SerializerMethodField()

    def _user(self, user, test_prep):
        if user is None:
            return None
        data = dict(DuelUserSerializer(user).data)
        if test_prep == 'sat':
            data['elo_rating'] = user.profile.elo_rating
        else:
            data['elo_rating'] = next(
                (row.duel_elo for row in user.test_prep_stats.all() if row.test_prep_id == test_prep),
                1500,
            )
        return data

    def get_user1(self, obj):
        return self._user(obj.user1, obj.test_prep_id)

    def get_user2(self, obj):
        return self._user(obj.user2, obj.test_prep_id)

    class Meta:
        model = Room
        fields = ['id', 'test_prep', 'user1', 'user2', 'created_at', 'status', 'questions', 'winner', 'battle_start_time',
                  'user1_score', 'user2_score', 'user1_elo_before', 'user1_elo_after',
                  'user2_elo_before', 'user2_elo_after']


class TrackedQuestionSerializer(serializers.ModelSerializer):
    question = QuestionSerializer()

    class Meta:
        model = TrackedQuestion
        fields = '__all__'


class TrackedQuestionResultSerializer(serializers.ModelSerializer):
    user = DuelUserSerializer()

    class Meta:
        model = TrackedQuestion
        fields = ['id', 'user', 'question', 'status']


class FriendRequestSerializer(serializers.ModelSerializer):
    from_user = UserSerializer()
    to_user = UserSerializer()

    class Meta:
        model = FriendRequest
        fields = ['id', 'from_user', 'to_user', 'timestamp', 'status']


class DirectMessageSerializer(serializers.ModelSerializer):
    sender_id = serializers.IntegerField(source='sender.id', read_only=True)
    recipient_id = serializers.IntegerField(source='recipient.id', read_only=True)

    class Meta:
        model = DirectMessage
        fields = ['id', 'sender_id', 'recipient_id', 'content', 'created_at', 'read_at']


class TournamentSerializer(serializers.ModelSerializer):
    participantNumber = serializers.IntegerField(read_only=True)
    questionNumber = serializers.IntegerField(read_only=True)
    # TournamentCard has always rendered `tournament.status`, which nothing sent,
    # so every card claimed to be ACTIVE — including rounds that closed weeks ago.
    status = serializers.SerializerMethodField()

    class Meta:
        model = Tournament
        fields = ['id', 'name', 'description', 'duration', 'start_time', 'end_time', 'participantNumber',
                  'questionNumber', 'private', 'join_code', 'status']

    def get_status(self, obj):
        now = timezone.now()
        if obj.start_time and now < obj.start_time:
            return 'upcoming'
        if obj.end_time and now >= obj.end_time:
            return 'ended'
        return 'active'


class TournamentParticipationSerializer(serializers.ModelSerializer):
    tournament = TournamentSerializer()

    class Meta:
        model = TournamentParticipation
        fields = ['id', 'user', 'tournament', 'start_time', 'end_time', 'score', 'last_correct_submission', 'status']


class TournamentQuestionSerializer(serializers.ModelSerializer):
    question = QuestionSerializer()

    class Meta:
        model = TournamentQuestion
        fields = ['id', 'participation', 'question', 'status', 'selected_choice', 'time_taken']


class TPSubmitAnswerSerializer(serializers.ModelSerializer):
    tournament_questions = serializers.SerializerMethodField()
    user = UserSerializer()

    class Meta:
        model = TournamentParticipation
        fields = ['id', 'user', 'score', 'last_correct_submission', 'tournament_questions']

    def get_tournament_questions(self, obj):
        # Only people in the round see the per-question grid — see
        # tournament_leaderboard for why. Others still get score and rank.
        if not self.context.get('show_questions'):
            return []
        questions = sorted(obj.tournamentquestion_set.all(), key=lambda q: q.id)
        return TournamentQuestionSerializer(questions, many=True).data
