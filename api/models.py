from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
import math
import random
import uuid
import pytz


FREE_DUEL_EMOJIS = (
    '👍', '🔥', '😂', '😮', '🎉', '💀', '👀', '🧠', '💪', '😎',
    '🤔', '😭', '🫡', '🚀', '⚡', '🎯', '🏆', '🤝', '😅', '🙃',
    '😤', '🥳', '🤯', '👏', '✨', '😈', '🐐', '✅', '❌', '🫠',
)
PREMIUM_DUEL_EMOJIS = ('🤡', '👎', '🗑️', '💩', '🤓', '🥱', '😏', '🤬', '🥶', '🥴')
DUEL_EMOJIS = FREE_DUEL_EMOJIS + PREMIUM_DUEL_EMOJIS


def default_duel_emotes():
    return list(FREE_DUEL_EMOJIS[:4])


def usable_duel_emotes(profile):
    """Return a four-emote loadout, excluding Premium choices after expiry."""
    allowed = set(FREE_DUEL_EMOJIS)
    if profile.has_premium:
        allowed.update(PREMIUM_DUEL_EMOJIS)
    loadout = [emoji for emoji in profile.duel_emotes if emoji in allowed]
    for emoji in default_duel_emotes():
        if len(loadout) == 4:
            break
        if emoji not in loadout:
            loadout.append(emoji)
    return loadout[:4]


# =========================================================
# Core Learning Models
# =========================================================

DEFAULT_TEST_PREP = 'sat'


class TestPrep(models.Model):
    """A test-prep product whose question bank and rankings are isolated."""
    code = models.SlugField(max_length=32, primary_key=True)
    name = models.CharField(max_length=80, unique=True)
    active = models.BooleanField(default=False, db_index=True)
    display_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['display_order', 'name']

    def __str__(self):
        return self.name


class TestSection(models.Model):
    """A configurable section/subject within a test (for example ACT Science)."""
    test_prep = models.ForeignKey(TestPrep, on_delete=models.CASCADE, related_name='sections')
    code = models.SlugField(max_length=32)
    name = models.CharField(max_length=80)
    active = models.BooleanField(default=True, db_index=True)
    display_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['test_prep__display_order', 'display_order', 'name']
        constraints = [
            models.UniqueConstraint(fields=['test_prep', 'code'], name='unique_section_per_test_prep'),
        ]

    def __str__(self):
        return f'{self.test_prep.name} — {self.name}'


class Question(models.Model):
    """Model representing a learning question with multiple choice answers."""
    SOURCE_SAT_QUESTION_BANK = 'sat_question_bank'
    SOURCE_OFFICIAL_QUESTION_BANK = 'official_question_bank'
    SOURCE_AI_GENERATED = 'ai_generated'
    SOURCE_OTHER = 'other'
    SOURCE_CHOICES = [
        (SOURCE_SAT_QUESTION_BANK, 'SAT Question Bank'),
        (SOURCE_OFFICIAL_QUESTION_BANK, 'Official Question Bank'),
        (SOURCE_AI_GENERATED, 'AI Generated'),
        (SOURCE_OTHER, 'Other'),
    ]

    question = models.TextField(null=False, blank=False)
    choice_a = models.CharField(max_length=1000)
    choice_b = models.CharField(max_length=1000)
    choice_c = models.CharField(max_length=1000)
    choice_d = models.CharField(max_length=1000)
    answer = models.CharField(max_length=1, choices=[('A', 'A'), ('B', 'B'), ('C', 'C'), ('D', 'D')])
    difficulty = models.IntegerField(choices=[(i, str(i)) for i in range(1, 6)], db_index=True)
    question_type = models.CharField(max_length=1000, null=True, blank=True, db_index=True)
    source = models.CharField(max_length=32, choices=SOURCE_CHOICES, default=SOURCE_OTHER, db_index=True)
    source_other = models.CharField(max_length=255, blank=True)
    explanation = models.TextField(null=True, blank=True)
    sp_elo_rating = models.IntegerField(default=0)
    test_prep = models.ForeignKey(
        TestPrep, on_delete=models.PROTECT, related_name='questions', default=DEFAULT_TEST_PREP,
    )
    subject = models.CharField(max_length=32, default='english', db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=['test_prep', 'subject', 'question_type', 'difficulty']),
        ]

    def __str__(self):
        return self.question

    def save(self, *args, **kwargs):
        from api.generation import normalize_question_type
        self.question_type = normalize_question_type(self.question_type)
        # Legacy SAT callers only supplied question_type. Keep those imports
        # working while new banks set test_prep + subject explicitly.
        if self.test_prep_id == DEFAULT_TEST_PREP:
            from api.generation import subject_of_type
            self.subject = subject_of_type(self.question_type)
        if self.source != self.SOURCE_OTHER:
            self.source_other = ''

        # If this is a newly created object with no Elo yet, initialize based on difficulty
        if self.pk is None and self.sp_elo_rating == 0:
            if self.difficulty == 1:
                self.sp_elo_rating = 600
            elif self.difficulty == 2:
                self.sp_elo_rating = 800
            elif self.difficulty == 3:
                self.sp_elo_rating = 1200
            elif self.difficulty == 4:
                self.sp_elo_rating = 1600
            elif self.difficulty == 5:
                self.sp_elo_rating = 2000

        super().save(*args, **kwargs)

    @property
    def answer_text(self):
        """Returns the text of the correct answer."""
        choices = {
            'A': self.choice_a,
            'B': self.choice_b,
            'C': self.choice_c,
            'D': self.choice_d,
        }
        return choices.get(self.answer, "Unknown choice")

    @classmethod
    def get_random_questions(self, num_questions, test_prep=DEFAULT_TEST_PREP):
        default_question_types = [
            'Cross-Text Connections', 'Text Structure and Purpose', 'Words in Context',
            'Rhetorical Synthesis', 'Transitions', 'Central Ideas and Details',
            'Command of Evidence', 'Inferences', 'Boundaries', 'Form, Structure, and Sense'
        ]
        questions = self.objects.filter(test_prep_id=test_prep)
        if test_prep == DEFAULT_TEST_PREP:
            questions = questions.filter(question_type__in=default_question_types)
        questions = list(questions)
        if num_questions > len(questions):
            num_questions = len(questions)
        return random.sample(questions, num_questions)


class QuestionReport(models.Model):
    REASON_CHOICES = [
        ('incorrect_statement', 'Incorrect problem statement'),
        ('no_correct_choice', 'No correct answer choice'),
        ('incorrect_answer', 'Incorrect marked answer'),
        ('bad_explanation', 'Bad or unclear explanation'),
        ('other', 'Other issue'),
    ]

    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name='reports')
    reporter = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='question_reports')
    reason = models.CharField(max_length=32, choices=REASON_CHOICES)
    details = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Q{self.question_id}: {self.get_reason_display()}"


class Announcement(models.Model):
    """The single site-wide announcement shown in the signed-in app."""
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    message = models.TextField(max_length=500, blank=True)
    is_active = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.message[:80] or 'Site announcement'


# =========================================================
# User Profile and Statistics Models
# =========================================================

class SATExamDate(models.Model):
    """Weekend SAT dates shown during onboarding, maintained in Django admin."""
    date = models.DateField(unique=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ['date']

    def __str__(self):
        return f'{self.date:%B} {self.date.day}, {self.date.year}'


class PendingRegistration(models.Model):
    """Unverified credentials waiting for proof that the email is owned."""
    email = models.EmailField(unique=True)
    grade = models.CharField(max_length=3)
    password_hash = models.CharField(max_length=128)
    verification_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    terms_accepted_at = models.DateTimeField()
    next_path = models.CharField(max_length=500, blank=True)
    email_sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.email


def current_school_year():
    """Return the year in which the current September-starting school year began."""
    today = timezone.localdate()
    return today.year if today.month >= 9 else today.year - 1


class Profile(models.Model):
    """Extended user profile with additional attributes and game statistics."""
    AVATAR_CHOICES = [
        ('violet', 'Violet'),
        ('sky', 'Sky'),
        ('emerald', 'Emerald'),
        ('amber', 'Amber'),
        ('rose', 'Rose'),
        ('slate', 'Slate'),
    ]
    AVATAR_ICON_CHOICES = [
        ('initial', 'Initial'),
        ('nova-quill', 'Nova Quill'),
        ('ember-abacus', 'Ember Abacus'),
        ('cipher-lantern', 'Cipher Lantern'),
        ('prism-page', 'Prism Page'),
        ('orbit-scout', 'Orbit Scout'),
        ('inkcap-alchemist', 'Inkcap Alchemist'),
        ('bloom-circuit', 'Bloom Circuit'),
        ('echo-fencer', 'Echo Fencer'),
        ('slate-sentinel', 'Slate Sentinel'),
        ('mira-mnemonic', 'Mira Mnemonic'),
        ('pixel-pathfinder', 'Pixel Pathfinder'),
        ('margin-warden', 'Margin Warden'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    biography = models.TextField(blank=True, null=True)
    grade = models.CharField(
        max_length=3,
        choices=[(str(i), str(i)) for i in range(8, 13)] + [('>12', '>12')],
        default='11'
    )
    grade_last_promoted_year = models.PositiveSmallIntegerField(
        default=current_school_year,
        help_text='September school-year start most recently applied to this grade.',
    )
    role = models.CharField(
        max_length=7,
        choices=[('STUDENT', 'Student'), ('TEACHER', 'Teacher')],
        default='STUDENT'
    )
    
    friends = models.ManyToManyField(User, related_name='friends', blank=True)
    elo_rating = models.IntegerField(default=1500)  # Duel ELO rating
    country = models.CharField(max_length=2, default='US')
    avatar = models.CharField(max_length=32, choices=AVATAR_CHOICES, default='violet')
    avatar_icon = models.CharField(max_length=32, choices=AVATAR_ICON_CHOICES, default='initial')
    # Bot profiles use normal User rows so they work with rooms, Elo, avatars,
    # and history. Email/notification jobs must always exclude this flag.
    is_bot = models.BooleanField(default=False, db_index=True)
    duel_emotes = models.JSONField(default=default_duel_emotes)
    max_streak = models.IntegerField(default=0)
    active_test_prep = models.ForeignKey(
        TestPrep, on_delete=models.PROTECT, related_name='active_profiles', default=DEFAULT_TEST_PREP,
    )
    goal = models.CharField(max_length=255,
                            choices=[('beginner', 'Beginner Path'), ('intermediate', 'Steady Learner'),
                                     ('advanced', 'Advanced Track'), ('expert', 'Expert Challenge')],
                            default='beginner')
    my_tournaments = models.ManyToManyField('api.Tournament', related_name='my_tournaments', blank=True)
    timezone = models.CharField(
        max_length=50,
        default='UTC',
        choices=[(tz, tz) for tz in pytz.all_timezones]
    )
    # Premium tier: flag + optional expiry. A null premium_until means
    # "until manually revoked" (e.g. lifetime or admin-granted).
    is_premium = models.BooleanField(default=False)
    premium_until = models.DateTimeField(null=True, blank=True)
    stripe_customer_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    stripe_subscription_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    stripe_price_id = models.CharField(max_length=255, blank=True, null=True)
    username_changed_at = models.DateTimeField(null=True, blank=True)
    username_finalized = models.BooleanField(default=True)
    grade_selected = models.BooleanField(default=True)

    # Account setup and communication preferences. `sat_exam_date_selected`
    # distinguishes "I don't know yet" from an existing account that has not
    # answered the question.
    sat_exam_date = models.DateField(null=True, blank=True)
    sat_exam_date_selected = models.BooleanField(default=False)
    marketing_opt_in = models.BooleanField(null=True, blank=True, default=None)
    terms_accepted_at = models.DateTimeField(null=True, blank=True)

    # Day streak: completing the daily practice goal (DAILY_PRACTICE_GOAL
    # answers in the user's local day) extends it. Evaluated lazily — a missed
    # day reads as 0 without any scheduled job (see practice_views).
    practice_streak = models.IntegerField(default=0)
    longest_practice_streak = models.IntegerField(default=0)
    last_practice_completed = models.DateField(null=True, blank=True)

    @property
    def has_premium(self):
        if not self.is_premium:
            return False
        return self.premium_until is None or self.premium_until > timezone.now()

    @property
    def onboarding_required(self):
        return (
            not self.username_finalized
            or not self.grade_selected
            or not self.sat_exam_date_selected
            or self.marketing_opt_in is None
            or self.terms_accepted_at is None
        )

    def promote_grade_for_school_year(self, today=None):
        """Advance a student's grade once per September, catching up missed years."""
        today = today or timezone.localdate()
        school_year = today.year if today.month >= 9 else today.year - 1
        if self.grade_last_promoted_year >= school_year:
            return False

        grades = ['8', '9', '10', '11', '12', '>12']
        current_index = grades.index(self.grade) if self.grade in grades else 0
        years = school_year - self.grade_last_promoted_year
        self.grade = grades[min(current_index + years, len(grades) - 1)]
        self.grade_last_promoted_year = school_year
        self.save(update_fields=['grade', 'grade_last_promoted_year'])
        return True

    def sigma(self, r, kappa, s=400):
        """
        Calculate the sigma function used in the Elo-Davidson model.
        """
        exponent = 10 ** (r / s)
        return exponent / (10 ** (-r / s) + kappa + exponent)

    def g_function(self, r, kappa, s=400):
        """
        Calculate the g(r; kappa) function.
        """
        exponent = 10 ** (r / s)
        return (exponent + kappa / 2) / (10 ** (-r / s) + kappa + exponent)

    def f(self, result, elo1, elo2, kappa=1, k=32):
        """
        Update Elo ratings based on the result using the Elo-Davidson model.

        Parameters:
        result (float): 1 for win, 0.5 for draw, 0 for loss (Player 1's perspective).
        elo1 (float): Player 1's Elo rating before the game.
        elo2 (float): Player 2's Elo rating before the game.
        kappa (float): Parameter controlling draw probability. Default is 1.
        k (float): Learning rate or K-factor. Default is 32.

        Returns:
        new_elo1 (float): Player 1's updated Elo rating.
        new_elo2 (float): Player 2's updated Elo rating.
        """
        # Rating difference
        r_ab = elo1 - elo2

        # Expected score for player 1
        E1 = self.g_function(r_ab, kappa)
        E2 = 1 - E1  # Expected score for player 2

        # Update ratings
        new_elo1 = elo1 + k * (result - E1)
        new_elo2 = elo2 + k * ((1 - result) - E2)

        return new_elo1, new_elo2

    def update_elo(self, opponent_elo, result):
        # k = 32  # K-factor for ELO calculation
        # expected_score = 1 / (1 + 10 ** ((opponent_elo - self.elo_rating) / 400))
        # new_elo = self.elo_rating + k * (result - expected_score)
        # self.elo_rating = int(new_elo)
        # self.save()
        result = result  # Draw
        elo1 = self.elo_rating  # Player 1's initial rating
        elo2 = opponent_elo  # Player 2's initial rating
        kappa = 1  # Default draw adjustment parameter
        k = 16  # K-factor - adjust for how much it fluctuates after a result

        new_elo1, new_elo2 = self.f(result, elo1, elo2, kappa, k)
        self.elo_rating = int(new_elo1)
        self.save()

    def __str__(self):
        return f"{self.user.username}'s Profile"


class PracticeStats(models.Model):
    """Per-user, per-test, per-subject practice state and lifetime counters.
    One row per (user, test prep, subject), so adding a new subject is a data change,
    not a schema change. Accuracy is derived (correct / answered), never
    stored, so it can't drift. In-progress questions live in
    PracticeActiveQuestion (one per lane, not per subject)."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='practice_stats')
    test_prep = models.ForeignKey(
        TestPrep, on_delete=models.CASCADE, related_name='practice_stats', default=DEFAULT_TEST_PREP,
    )
    subject = models.CharField(max_length=32)
    elo = models.IntegerField(default=1200)
    answered = models.IntegerField(default=0)
    correct = models.IntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'test_prep', 'subject'], name='unique_practice_stats_per_test_subject',
            ),
        ]

    @property
    def accuracy(self):
        return self.correct / self.answered if self.answered else None

    def __str__(self):
        return f"{self.user.username} {self.subject}: elo {self.elo}, {self.correct}/{self.answered}"


class PracticeTypeStats(models.Model):
    """Per-user, per-test, per-question-type progress through the question bank.
    `solved` counts DISTINCT questions attempted (practice never re-serves an
    attempted question, so attempted == progress toward finishing the type);
    `correct` counts how many of those were answered right. Derived from
    PracticeAttempt: the backfill migration rebuilds both from the attempt
    log, so pre-log legacy activity intentionally starts at zero here."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='practice_type_stats')
    test_prep = models.ForeignKey(
        TestPrep, on_delete=models.CASCADE, related_name='practice_type_stats', default=DEFAULT_TEST_PREP,
    )
    subject = models.CharField(max_length=32, default='english')
    question_type = models.CharField(max_length=1000)
    solved = models.IntegerField(default=0)
    correct = models.IntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'test_prep', 'subject', 'question_type'],
                name='unique_practice_type_stats_per_test',
            ),
        ]

    def __str__(self):
        return f"{self.user.username} [{self.question_type}]: {self.correct}/{self.solved}"


class PracticeActiveQuestion(models.Model):
    """The in-progress practice question, one per lane. A lane is either a
    subject's random mix ('english:any' / 'math:any') or a specific question
    type for premium topic drills, so switching lanes and back resumes the
    same question instead of letting it be skipped. Rows are deleted when the
    question is answered, so the table only holds open questions."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='practice_active_questions')
    test_prep = models.ForeignKey(
        TestPrep, on_delete=models.CASCADE, related_name='active_questions', default=DEFAULT_TEST_PREP,
    )
    lane = models.CharField(max_length=128)
    question = models.ForeignKey('api.Question', on_delete=models.CASCADE, related_name='+')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'test_prep', 'lane'], name='unique_active_question_per_test_lane',
            ),
        ]

    def __str__(self):
        return f"{self.user.username} [{self.lane}] -> Q{self.question_id}"


class UserStatistics(models.Model):
    """Account-wide shop economy state.

    Practice counters used to live here too; they moved to PracticeStats
    (per-subject), and duplicate rows were merged when this became OneToOne.
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='infinitequestionstatistics')
    coins = models.IntegerField(default=0)
    normal_multiplier = models.FloatField(default=1.00)

    def __str__(self):
        return f"{self.user.username} - {self.coins} coins"

    def total_multiplier(self):
        return round(self.normal_multiplier, 2)


class TestPrepUserStats(models.Model):
    """Per-user totals that must never leak between test-prep products."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='test_prep_stats')
    test_prep = models.ForeignKey(TestPrep, on_delete=models.CASCADE, related_name='user_stats')
    duel_elo = models.IntegerField(default=1500)
    max_streak = models.IntegerField(default=0)
    practice_streak = models.IntegerField(default=0)
    longest_practice_streak = models.IntegerField(default=0)
    last_practice_completed = models.DateField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'test_prep'], name='unique_user_stats_per_test_prep'),
        ]
        indexes = [models.Index(fields=['test_prep', '-duel_elo'])]

    def __str__(self):
        return f'{self.user.username} — {self.test_prep_id}: {self.duel_elo}'

    @classmethod
    def for_user(cls, user, test_prep=DEFAULT_TEST_PREP):
        default_elo = user.profile.elo_rating if test_prep == DEFAULT_TEST_PREP else 1500
        stats, _ = cls.objects.get_or_create(
            user=user, test_prep_id=test_prep,
            defaults={'duel_elo': default_elo, 'max_streak': user.profile.max_streak},
        )
        if test_prep == DEFAULT_TEST_PREP and stats.duel_elo != user.profile.elo_rating:
            stats.duel_elo = user.profile.elo_rating
            stats.save(update_fields=['duel_elo'])
        return stats

    def update_elo(self, opponent_elo, result):
        new_elo, _ = self.user.profile.f(result, self.duel_elo, opponent_elo, kappa=1, k=16)
        self.duel_elo = int(new_elo)
        self.save(update_fields=['duel_elo'])
        # Old clients still read Profile.elo_rating for SAT.
        if self.test_prep_id == DEFAULT_TEST_PREP:
            Profile.objects.filter(user_id=self.user_id).update(elo_rating=self.duel_elo)



class SurvivalStatistics(models.Model):
    """Tracks user's survival mode performance."""
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    test_prep = models.ForeignKey(
        TestPrep, on_delete=models.CASCADE, related_name='survival_stats', default=DEFAULT_TEST_PREP,
    )
    record = models.IntegerField(default=0)

    def __str__(self):
        return f"{self.user.username} - {self.record}"


# =========================================================
# Game Mechanics Models
# =========================================================

class Tournament(models.Model):
    """Represents a tournament event with multiple participants."""
    name = models.CharField(max_length=255)
    description = models.TextField()
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True)
    duration = models.DurationField(default=timezone.timedelta(minutes=30))
    questions = models.ManyToManyField(Question)
    test_prep = models.ForeignKey(
        TestPrep, on_delete=models.PROTECT, related_name='tournaments', default=DEFAULT_TEST_PREP,
    )
    private = models.BooleanField(default=False)
    join_code = models.CharField(max_length=10, blank=True, null=True)

    def __str__(self):
        return self.name

    @property
    def questionNumber(self):
        return self.questions.count()

    @property
    def participantNumber(self):
        return self.tournamentparticipation_set.count()


class TournamentParticipation(models.Model):
    """Tracks user participation in tournaments."""
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE)
    start_time = models.DateTimeField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)
    score = models.IntegerField(default=0)
    last_correct_submission = models.DurationField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=[('Active', 'Active'), ('Completed', 'Completed')])

    def __str__(self):
        return f"{self.user.username} in {self.tournament.name}"

    @property
    def is_live(self):
        """True while the run is still on the clock.

        A run is only marked Completed when something calls finish/, which for
        a long time meant "the browser tab happened to be open when the timer
        hit zero". Abandoned runs stayed Active forever and kept hiding their
        questions from every review surface, so liveness is derived from the
        deadline rather than trusted from the column.
        """
        return self.status == 'Active' and (self.end_time is None or timezone.now() < self.end_time)

    def expire_if_over(self):
        """Close out a run whose clock ran out while nobody was watching."""
        if self.status == 'Active' and self.end_time and timezone.now() >= self.end_time:
            self.status = 'Completed'
            self.save(update_fields=['status'])
        return self

    @staticmethod
    def live_for(user):
        """Runs `user` is genuinely still competing in — the anti-cheat gate.

        A row with no end_time has no deadline to have passed, so it counts as
        live: this gate fails closed.
        """
        return TournamentParticipation.objects.filter(
            models.Q(end_time__gt=timezone.now()) | models.Q(end_time__isnull=True),
            user=user, status='Active',
        )


class TournamentQuestion(models.Model):
    participation = models.ForeignKey(TournamentParticipation, on_delete=models.CASCADE)
    question = models.ForeignKey(Question, on_delete=models.CASCADE)
    status = models.CharField(max_length=10,
                              choices=[('Correct', 'Correct'), ('Incorrect', 'Incorrect'), ('Blank', 'Blank')])
    # What the user actually picked, so review can show their answer next to the
    # correct one instead of only saying "Incorrect".
    selected_choice = models.CharField(max_length=1000, blank=True, default='')
    time_taken = models.DurationField(blank=True, null=True)  # Time taken to answer from start of participation

    def __str__(self):
        return f"{self.participation.user.username} - Q{self.question.id} - {self.status} - {self.time_taken}"


class Room(models.Model):
    """Represents a battle room between two users."""
    user1 = models.ForeignKey(User, related_name='room_user1', on_delete=models.CASCADE)
    test_prep = models.ForeignKey(
        TestPrep, on_delete=models.PROTECT, related_name='duel_rooms', default=DEFAULT_TEST_PREP,
    )
    user2 = models.ForeignKey(User, related_name='room_user2', on_delete=models.CASCADE, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    questions = models.ManyToManyField(Question, blank=True)
    status = models.CharField(max_length=10, db_index=True,
                              choices=[('Searching', 'Searching'), ('Battling', 'Battling'), ('Ended', 'Ended')])
    battle_start_time = models.DateTimeField(null=True, blank=True)
    battle_duration = models.IntegerField(default=300)  # Duration in seconds, default 5 minutes
    winner = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    user1_score = models.IntegerField(default=0)
    user2_score = models.IntegerField(default=0)
    user1_elo_before = models.IntegerField(null=True, blank=True)
    user1_elo_after = models.IntegerField(null=True, blank=True)
    user2_elo_before = models.IntegerField(null=True, blank=True)
    user2_elo_after = models.IntegerField(null=True, blank=True)

    def is_full(self):
        return self.user2 is not None

    def is_battle_ended(self):
        if self.battle_start_time and self.status == 'Battling':
            return timezone.now() > self.battle_start_time + timezone.timedelta(seconds=self.battle_duration)
        return False

    def __str__(self):
        return f"Room {self.id} by {self.user1.username} and {self.user2.username if self.user2 else 'empty'}"

    def end_battle(self):
        if not self.user2:
            return

        if self.user1_score > self.user2_score:
            self.winner = self.user1
            result_user1, result_user2 = 1, 0
        elif self.user2_score > self.user1_score:
            self.winner = self.user2
            result_user1, result_user2 = 0, 1
        else:
            self.winner = None
            result_user1 = result_user2 = 0.5

        user1_profile = self.user1.profile
        user2_profile = self.user2.profile
        user1_stats = TestPrepUserStats.for_user(self.user1, self.test_prep_id)
        user2_stats = TestPrepUserStats.for_user(self.user2, self.test_prep_id)
        user1_start = user1_stats.duel_elo
        user2_start = user2_stats.duel_elo
        self.user1_elo_before = user1_start
        self.user2_elo_before = user2_start
        self.status = 'Ended'
        self.save()

        user1_stats.update_elo(user2_start, result_user1)
        user2_stats.update_elo(user1_start, result_user2)
        for profile, stats in ((user1_profile, user1_stats), (user2_profile, user2_stats)):
            if profile.is_bot and stats.duel_elo > 1799:
                stats.duel_elo = 1799
                stats.save(update_fields=['duel_elo'])
                if self.test_prep_id == DEFAULT_TEST_PREP:
                    profile.elo_rating = 1799
                    profile.save(update_fields=['elo_rating'])
        self.user1_elo_after = user1_stats.duel_elo
        self.user2_elo_after = user2_stats.duel_elo
        Room.objects.filter(pk=self.pk).update(
            user1_elo_after=self.user1_elo_after,
            user2_elo_after=self.user2_elo_after,
        )

    def save(self, *args, **kwargs):
        previous_status = None
        if self.pk:
            previous_status = Room.objects.filter(pk=self.pk).values_list('status', flat=True).first()

        super().save(*args, **kwargs)
        if self.status == 'Ended' and previous_status != 'Ended':
            self.end_battle()
        if not self.questions.exists() and self.user1 and self.user2:
            self.questions.set(Question.get_random_questions(10, self.test_prep_id))
            for question in self.questions.all():
                TrackedQuestion.objects.create(
                    user=self.user1,
                    room=self,
                    question=question,
                    status="Blank"
                )
                TrackedQuestion.objects.create(
                    user=self.user2,
                    room=self,
                    question=question,
                    status="Blank"
                )


class TrackedQuestion(models.Model):
    """Tracks individual question attempts in rooms."""
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    room = models.ForeignKey(Room, on_delete=models.CASCADE)
    question = models.ForeignKey(Question, on_delete=models.CASCADE)
    status = models.CharField(max_length=10,
                              choices=[('Correct', 'Correct'), ('Incorrect', 'Incorrect'), ('Blank', 'Blank')])
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - {self.question.question} - {self.status}"


class DuelEmote(models.Model):
    """A lightweight live reaction targeting either a duel or party room."""
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name='emotes', null=True, blank=True)
    party_room = models.ForeignKey(
        'PartyRoom', on_delete=models.CASCADE, related_name='emotes', null=True, blank=True,
    )
    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name='duel_emotes')
    emoji = models.CharField(max_length=8)
    created_at = models.DateTimeField(auto_now_add=True)
    visible_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['visible_at', 'id']
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(room__isnull=False, party_room__isnull=True)
                    | models.Q(room__isnull=True, party_room__isnull=False)
                ),
                name='reaction_targets_exactly_one_room',
            ),
        ]

    def __str__(self):
        target = f'duel {self.room_id}' if self.room_id else f'party {self.party_room_id}'
        return f"{self.sender.username} {self.emoji} in {target}"


class FriendRequest(models.Model):
    """Handles friend connections between users."""
    from_user = models.ForeignKey(User, related_name='sent_friend_requests', on_delete=models.CASCADE)
    to_user = models.ForeignKey(User, related_name='received_friend_requests', on_delete=models.CASCADE)
    timestamp = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=10,
                              choices=[('pending', 'Pending'), ('accepted', 'Accepted'), ('rejected', 'Rejected')],
                              default='pending')

    def __str__(self):
        return f"Friend request from {self.from_user} to {self.to_user}"

    def accept(self):
        self.status = 'accepted'
        self.save()
        self.from_user.profile.friends.add(self.to_user)
        self.to_user.profile.friends.add(self.from_user)

    def reject(self):
        self.status = 'rejected'
        self.save()


class DirectMessage(models.Model):
    """A one-to-one message between two students.

    Conversations are not modelled separately: a thread is every row where the
    two users appear as sender/recipient in either direction, which keeps the
    friends list (small, already loaded) the source of truth for who you can
    talk to. Sending requires an active friendship; removing a friend deletes
    the pair's rows as part of that explicit user action.
    """
    MAX_LENGTH = 2000

    sender = models.ForeignKey(User, related_name='sent_messages', on_delete=models.CASCADE)
    recipient = models.ForeignKey(User, related_name='received_messages', on_delete=models.CASCADE)
    content = models.TextField(max_length=MAX_LENGTH)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_at', 'id']
        indexes = [
            # Thread reads and unread counts both filter on the pair of users.
            models.Index(fields=['sender', 'recipient', 'created_at']),
            models.Index(fields=['recipient', 'read_at']),
        ]

    def __str__(self):
        return f"{self.sender} → {self.recipient} at {self.created_at:%Y-%m-%d %H:%M}"


# =========================================================
# Tracking and Progress Models
# =========================================================

class OnlineUser(models.Model):
    """Tracks user online status."""
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    last_seen = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return self.user.username


class PracticeAttempt(models.Model):
    """One infinite-practice answer submission.

    The source of truth for the daily free-tier quota and for "only the first
    attempt at a question moves Elo".
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='practice_attempts')
    test_prep = models.ForeignKey(
        TestPrep, on_delete=models.CASCADE, related_name='practice_attempts', default=DEFAULT_TEST_PREP,
    )
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name='practice_attempts')
    subject = models.CharField(
        max_length=32,
        default='english',
        db_index=True,
    )
    correct = models.BooleanField()
    # Keep the answer text submitted at the time of the attempt. Older rows
    # predate answer-history, so they intentionally remain blank.
    selected_choice = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'created_at']),   # daily quota lookups
            models.Index(fields=['user', 'question']),      # first-attempt checks
            models.Index(fields=['user', 'test_prep', 'subject']),
        ]

    def __str__(self):
        return f"{self.user.username} - Q{self.question_id} - {'✓' if self.correct else '✗'}"


class PracticeTestResult(models.Model):
    """A completed full-length/diagnostic practice test.

    Stores the final score plus per-question outcomes so the result page can
    be reopened later; question text is re-fetched by id on review.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='practice_test_results')
    test_prep = models.ForeignKey(
        TestPrep, on_delete=models.PROTECT, related_name='legacy_test_results', default=DEFAULT_TEST_PREP,
    )
    test_id = models.IntegerField(default=1)
    test_name = models.CharField(max_length=100, default='SAT Diagnostic Test')
    score = models.IntegerField()
    correct = models.IntegerField()
    total = models.IntegerField()
    time_used_seconds = models.IntegerField(null=True, blank=True)
    # [{"question_id": int, "user_choice": "A".."D" | null, "correct": bool}, ...]
    questions = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['user', 'created_at']),   # history, newest-first
        ]

    def __str__(self):
        return f"{self.user.username} - {self.test_name} - {self.score}"


class PracticeTestModule(models.Model):
    """An isolated, generated SAT module and its private question set."""
    SUBJECT_CHOICES = [('english', 'Reading and Writing'), ('math', 'Math')]
    ROUTE_CHOICES = [
        ('A', 'Module 1 (routing)'),
        ('B', 'Module 2 (lower difficulty)'),
        ('C', 'Module 2 (higher difficulty)'),
    ]

    name = models.CharField(max_length=120, unique=True)
    test_prep = models.ForeignKey(
        TestPrep, on_delete=models.PROTECT, related_name='adaptive_modules', default=DEFAULT_TEST_PREP,
    )
    subject = models.CharField(max_length=10, choices=SUBJECT_CHOICES)
    route = models.CharField(max_length=1, choices=ROUTE_CHOICES)
    questions = models.JSONField(default=list)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='created_practice_test_modules',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-id']

    @property
    def question_count(self):
        return len(self.questions)

    def __str__(self):
        return self.name


class PracticeTest(models.Model):
    """A published adaptive test assembled from exclusive A/B/C modules."""

    TYPE_FULL = 'full'
    TYPE_ENGLISH = 'english'
    TYPE_MATH = 'math'
    TYPE_CHOICES = [
        (TYPE_FULL, 'Full SAT'),
        (TYPE_ENGLISH, 'Reading and Writing only'),
        (TYPE_MATH, 'Math only'),
    ]
    TYPE_SUBJECTS = {
        TYPE_FULL: ('english', 'math'),
        TYPE_ENGLISH: ('english',),
        TYPE_MATH: ('math',),
    }

    name = models.CharField(max_length=120, unique=True)
    test_prep = models.ForeignKey(
        TestPrep, on_delete=models.PROTECT, related_name='adaptive_tests', default=DEFAULT_TEST_PREP,
    )
    test_type = models.CharField(max_length=10, choices=TYPE_CHOICES, default=TYPE_FULL, db_index=True)
    premium_only = models.BooleanField(default=False, db_index=True)
    english_a = models.OneToOneField(
        PracticeTestModule, on_delete=models.PROTECT, related_name='practice_test_english_a',
        null=True, blank=True,
    )
    english_b = models.OneToOneField(
        PracticeTestModule, on_delete=models.PROTECT, related_name='practice_test_english_b',
        null=True, blank=True,
    )
    english_c = models.OneToOneField(
        PracticeTestModule, on_delete=models.PROTECT, related_name='practice_test_english_c',
        null=True, blank=True,
    )
    math_a = models.OneToOneField(
        PracticeTestModule, on_delete=models.PROTECT, related_name='practice_test_math_a',
        null=True, blank=True,
    )
    math_b = models.OneToOneField(
        PracticeTestModule, on_delete=models.PROTECT, related_name='practice_test_math_b',
        null=True, blank=True,
    )
    math_c = models.OneToOneField(
        PracticeTestModule, on_delete=models.PROTECT, related_name='practice_test_math_c',
        null=True, blank=True,
    )
    active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='created_practice_tests',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-id']

    def clean(self):
        expected = {
            'english_a': ('english', 'A'), 'english_b': ('english', 'B'), 'english_c': ('english', 'C'),
            'math_a': ('math', 'A'), 'math_b': ('math', 'B'), 'math_c': ('math', 'C'),
        }
        included_subjects = self.TYPE_SUBJECTS.get(self.test_type)
        if not included_subjects:
            raise ValidationError({'test_type': 'Choose a valid practice-test type.'})
        module_ids = []
        for field, signature in expected.items():
            module = getattr(self, field, None)
            required = signature[0] in included_subjects
            if required and module is None:
                raise ValidationError({field: 'This module is required for the selected test type.'})
            if not required and module is not None:
                raise ValidationError({field: 'This module must be empty for the selected test type.'})
            if module is None:
                continue
            module_ids.append(module.id)
            if module.test_prep_id != self.test_prep_id:
                raise ValidationError({field: 'This module belongs to a different test prep.'})
            if (module.subject, module.route) != signature:
                raise ValidationError({field: 'This module has the wrong subject or adaptive route.'})
        if len(module_ids) != len(set(module_ids)):
            raise ValidationError('Each practice-test slot must use a different module.')

    def save(self, *args, **kwargs):
        # Route validation plus the OneToOne constraints guarantee that a
        # module can never be reused by another valid test.
        self.clean()
        return super().save(*args, **kwargs)

    @property
    def included_subjects(self):
        return self.TYPE_SUBJECTS[self.test_type]

    @property
    def delivered_question_count(self):
        return sum(
            getattr(self, f'{subject}_a').question_count
            + max(getattr(self, f'{subject}_b').question_count, getattr(self, f'{subject}_c').question_count)
            for subject in self.included_subjects
        )

    @property
    def duration_minutes(self):
        return sum({'english': 64, 'math': 70}[subject] for subject in self.included_subjects)

    @property
    def maximum_score(self):
        return 1600 if self.test_type == self.TYPE_FULL else 800

    def __str__(self):
        return self.name


class PracticeTestAttempt(models.Model):
    """A resumable adaptive test sitting, including its server-owned score."""

    STATUS_ACTIVE = 'active'
    STATUS_COMPLETED = 'completed'
    STATUS_CHOICES = [(STATUS_ACTIVE, 'Active'), (STATUS_COMPLETED, 'Completed')]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='adaptive_test_attempts')
    practice_test = models.ForeignKey(PracticeTest, on_delete=models.PROTECT, related_name='attempts')
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_ACTIVE, db_index=True)
    phase = models.CharField(max_length=20, default='english_a')
    selected_routes = models.JSONField(default=dict)
    answers = models.JSONField(default=dict)
    review_questions = models.JSONField(default=dict)
    annotations = models.JSONField(default=dict)
    remaining_seconds = models.JSONField(default=dict)
    current_question = models.PositiveIntegerField(default=1)
    break_started_at = models.DateTimeField(null=True, blank=True)

    reading_writing_score = models.PositiveSmallIntegerField(null=True, blank=True)
    math_score = models.PositiveSmallIntegerField(null=True, blank=True)
    total_score = models.PositiveSmallIntegerField(null=True, blank=True)
    score_details = models.JSONField(default=dict)
    contributes_to_calibration = models.BooleanField(default=False, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'practice_test'],
                condition=models.Q(status='active'),
                name='one_active_attempt_per_user_test',
            ),
            models.UniqueConstraint(
                fields=['user', 'practice_test'],
                condition=models.Q(contributes_to_calibration=True),
                name='one_calibration_attempt_per_user_test',
            ),
        ]
        indexes = [
            models.Index(fields=['practice_test', 'status']),
            models.Index(fields=['user', 'status', 'created_at']),
        ]

    def __str__(self):
        return f'{self.user.username} - {self.practice_test.name} - {self.status}'


class SavedQuestion(models.Model):
    """A question the user marked for review from practice.

    `subject` is denormalized off the question's type the same way
    PracticeAttempt does it, so the saved list splits by subject without
    re-deriving the taxonomy on every read.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='saved_questions')
    test_prep = models.ForeignKey(
        TestPrep, on_delete=models.CASCADE, related_name='saved_questions', default=DEFAULT_TEST_PREP,
    )
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name='saved_by')
    subject = models.CharField(
        max_length=32,
        default='english',
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(fields=['user', 'question'], name='unique_saved_question_per_user'),
        ]
        indexes = [
            models.Index(fields=['user', 'subject']),      # saved list, split by subject
            models.Index(fields=['user', 'created_at']),   # newest-first paging
        ]

    def __str__(self):
        return f"{self.user.username} saved Q{self.question_id}"


# =========================================================
# Party Mode (Kahoot-style live rooms)
# =========================================================

PARTY_COUNTDOWN_SECONDS = 5
# Players polling within this window count as present; "everyone answered"
# ignores ghosts who closed the tab so one dropout can't stall the room.
PARTY_ACTIVE_WINDOW_SECONDS = 12
# A player who hasn't polled for this long has left (closed the tab, swiped
# back). Long enough to survive a refresh or brief app switch.
PARTY_PRESENCE_TIMEOUT_SECONDS = 30

PARTY_MAX_TEAMS = 4
PARTY_DEFAULT_TEAM_NAMES = ('Team A', 'Team B', 'Team C', 'Team D')
# How long players get to place a Final Jeopardy bet before the last question.
PARTY_WAGER_SECONDS = 30
# Survival: last-one-standing caps lives outright; a fixed-length game scales
# the cap to the question count so hearts stay scarce enough to matter.
PARTY_MAX_LIVES = 5


def party_lives_cap(last_standing, num_questions):
    if last_standing:
        return PARTY_MAX_LIVES
    return max(1, math.ceil(math.sqrt(max(1, num_questions))))


class PartyRoom(models.Model):
    """A live Kahoot-style quiz room.

    Clients poll the state endpoint; `advance()` derives phase transitions
    from timestamps on every read, so there is no background worker.
    """
    STATUSES = ('lobby', 'countdown', 'question', 'wager', 'leaderboard', 'playing', 'finished')
    MODES = ('classic', 'teams', 'survival', 'jeopardy', 'goldrush')

    host = models.ForeignKey(User, related_name='hosted_parties', on_delete=models.CASCADE)
    # `host` can move when the creator leaves; history still needs to remember
    # who originally made the room.
    original_host = models.ForeignKey(
        User, related_name='created_parties', null=True, blank=True, on_delete=models.SET_NULL,
    )
    test_prep = models.ForeignKey(
        TestPrep, on_delete=models.PROTECT, related_name='party_rooms', default=DEFAULT_TEST_PREP,
    )
    code = models.CharField(max_length=6, db_index=True)
    status = models.CharField(max_length=12, default='lobby',
                              choices=[(s, s) for s in STATUSES])
    mode = models.CharField(max_length=10, default='classic',
                            choices=[(m, m) for m in MODES])
    # Teams mode only. `team_names` is index-aligned with PartyPlayer.team.
    num_teams = models.IntegerField(default=2)
    random_teams = models.BooleanField(default=True)
    team_names = models.JSONField(default=list)
    # Survival mode only. `last_standing` plays until one player is left;
    # otherwise the room plays every question and the most hearts wins.
    lives = models.IntegerField(default=3)
    last_standing = models.BooleanField(default=True)
    max_players = models.IntegerField(default=6)
    num_questions = models.IntegerField(default=10)
    seconds_per_question = models.IntegerField(default=90)
    # Gold Rush only: total game length in seconds. Each player answers a
    # self-paced stream of questions until this runs out.
    time_limit = models.IntegerField(default=600)
    subject = models.CharField(max_length=32, default='mixed')
    difficulty = models.CharField(max_length=6, default='medium',
                                  choices=[(d, d) for d in ('easy', 'medium', 'hard')])
    question_ids = models.JSONField(default=list)  # ordered; index = question number - 1
    current_index = models.IntegerField(default=0)
    phase_started_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Party {self.code} by {self.host.username} ({self.status})"

    def current_question_id(self):
        if 0 <= self.current_index < len(self.question_ids):
            return self.question_ids[self.current_index]
        return None

    def question_deadline(self):
        return self.phase_started_at + timezone.timedelta(seconds=self.seconds_per_question)

    def wager_deadline(self):
        return self.phase_started_at + timezone.timedelta(seconds=PARTY_WAGER_SECONDS)

    def game_deadline(self):
        """Gold Rush: when the whole-room clock runs out."""
        return self.phase_started_at + timezone.timedelta(seconds=self.time_limit)

    def is_final_question(self):
        return self.current_index + 1 >= len(self.question_ids)

    def is_wager_question(self):
        """True while the room is on the Final Jeopardy betting question."""
        return self.mode == 'jeopardy' and self.is_final_question()

    def survivors(self, players=None):
        """Players still holding at least one heart."""
        roster = self.players.all() if players is None else players
        return [p for p in roster if p.lives > 0]

    def eliminates(self):
        """Only last-one-standing knocks players out.

        A fixed-length game keeps everyone playing and treats hearts as the
        score, so running out is a bad round rather than the end of the game.
        """
        return self.mode == 'survival' and self.last_standing

    def charge_survival_timeouts(self):
        """Take a heart from anyone still alive who let the clock run out.

        Sitting a question out has to cost the same as answering it wrong,
        or waiting the timer out becomes a way to dodge the risk.
        """
        key = str(self.current_index)
        for player in self.survivors():
            if key in player.answers:
                continue
            player.answers[key] = {'choice': None, 'correct': False, 'points': 0, 'life_lost': True}
            player.lives -= 1
            player.save(update_fields=['answers', 'lives'])

    def survival_is_over(self):
        """Last-one-standing ends as soon as the field is down to one player."""
        return self.mode == 'survival' and self.last_standing and len(self.survivors()) <= 1

    def game_is_over(self):
        return self.is_final_question() or self.survival_is_over()

    def settle_unplayed_wagers(self):
        """Charge bets to anyone who let the final question time out.

        Without this, sitting the question out would be strictly safer than
        answering it, which defeats the whole point of betting.
        """
        key = str(self.current_index)
        for player in self.players.all():
            if key in player.answers or not player.wager:
                continue
            player.answers[key] = {
                'choice': None, 'correct': False,
                'points': -player.wager, 'wager': player.wager,
            }
            player.score -= player.wager
            player.save(update_fields=['answers', 'score'])

    def team_label(self, index):
        names = self.team_names or []
        if 0 <= index < len(names) and str(names[index]).strip():
            return str(names[index])
        return PARTY_DEFAULT_TEAM_NAMES[index % PARTY_MAX_TEAMS]

    def assign_missing_teams(self):
        """Seat every teamless player on the smallest team.

        Covers both random assignment at kickoff and anyone the host left
        unsorted when they hit start, so nobody plays without a team.
        """
        counts = {i: 0 for i in range(self.num_teams)}
        unassigned = []
        for player in self.players.order_by('id'):
            if player.team is not None and 0 <= player.team < self.num_teams:
                counts[player.team] += 1
            else:
                unassigned.append(player)
        random.shuffle(unassigned)
        for player in unassigned:
            target = min(counts, key=lambda i: (counts[i], i))
            player.team = target
            counts[target] += 1
            player.save(update_fields=['team'])

    def sync_presence(self):
        """Reconcile the room with players who left without saying goodbye.

        Polling is our only presence signal, so this runs on every state read:
        stale lobby seats are freed, a vanished host hands the room to the
        longest-seated active player, and a room with nobody left is closed so
        its join code stops matching.
        """
        if self.status == 'finished':
            return
        cutoff = timezone.now() - timezone.timedelta(seconds=PARTY_PRESENCE_TIMEOUT_SECONDS)
        players = list(self.players.order_by('id'))
        active = [p for p in players if p.last_seen >= cutoff]
        if not active:
            self.status = 'finished'
            self.save(update_fields=['status'])
            return
        if self.status == 'lobby':
            # Mid-game seats survive a dropout (scores may still podium);
            # lobby seats don't, so the player count stays honest.
            self.players.filter(last_seen__lt=cutoff).delete()
        if all(p.user_id != self.host_id for p in active):
            self.host = active[0].user
            self.save(update_fields=['host'])

    def advance(self):
        """Move the room forward when the current phase has expired."""
        self.sync_presence()
        now = timezone.now()
        if self.status == 'countdown':
            ends = self.phase_started_at + timezone.timedelta(seconds=PARTY_COUNTDOWN_SECONDS)
            if now >= ends:
                # Gold Rush is self-paced: everyone plays at once under one clock.
                self.status = 'playing' if self.mode == 'goldrush' else 'question'
                self.phase_started_at = ends
                self.save(update_fields=['status', 'phase_started_at'])
        if self.status == 'playing':
            if now >= self.game_deadline():
                self.status = 'finished'
                self.save(update_fields=['status'])
        if self.status == 'wager':
            cutoff = now - timezone.timedelta(seconds=PARTY_ACTIVE_WINDOW_SECONDS)
            players = list(self.players.all())
            active = [p for p in players if p.last_seen >= cutoff] or players
            # A player with nothing to bet has nothing to decide, so they never block.
            if now >= self.wager_deadline() or all(p.wager_locked or p.score <= 0 for p in active):
                self.status = 'question'
                self.phase_started_at = now
                self.save(update_fields=['status', 'phase_started_at'])

        if self.status == 'question':
            key = str(self.current_index)
            cutoff = now - timezone.timedelta(seconds=PARTY_ACTIVE_WINDOW_SECONDS)
            players = list(self.players.all())
            active = [p for p in players if p.last_seen >= cutoff] or players
            if self.eliminates():
                # Knocked-out players are spectators — they can't answer, so
                # they must not hold the room open either.
                active = [p for p in active if p.lives > 0] or active
            if now >= self.question_deadline() or all(key in p.answers for p in active):
                if self.is_wager_question():
                    self.settle_unplayed_wagers()
                if self.mode == 'survival':
                    self.charge_survival_timeouts()
                self.status = 'leaderboard'
                self.phase_started_at = now
                self.save(update_fields=['status', 'phase_started_at'])


class PartyPlayer(models.Model):
    """A user's seat (and running score) in a party room. The host has one too."""
    room = models.ForeignKey(PartyRoom, related_name='players', on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    score = models.IntegerField(default=0)
    team = models.IntegerField(null=True, blank=True)  # teams mode; index into room.team_names
    lives = models.IntegerField(default=0)  # survival mode; 0 once eliminated
    # Final Jeopardy bet. `wager_locked` distinguishes a deliberate bet of 0
    # from a player who simply hasn't decided yet.
    wager = models.IntegerField(default=0)
    wager_locked = models.BooleanField(default=False)
    # question index (str) -> {'choice': 'A', 'correct': bool, 'points': int}
    answers = models.JSONField(default=dict)
    # Gold Rush only. Each player walks their own shuffled copy of the room's
    # question pool; `gq_pending` holds the current chest / wrong-answer screen.
    gq_deck = models.JSONField(default=list)  # shuffled question ids
    gq_index = models.IntegerField(default=0)
    gq_locked_until = models.DateTimeField(null=True, blank=True)  # 3s wrong-answer penalty
    gq_pending = models.JSONField(null=True, blank=True)  # {'kind': 'chest'|'wrong', ...}
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['id']
        constraints = [
            models.UniqueConstraint(fields=['room', 'user'], name='unique_party_player'),
        ]

    def __str__(self):
        return f"{self.user.username} in party {self.room.code} ({self.score})"

    def gold_question_id(self):
        """Current Gold Rush question, reshuffling the deck for another lap."""
        if not self.gq_deck:
            return None
        if self.gq_index >= len(self.gq_deck):
            random.shuffle(self.gq_deck)
            self.gq_index = 0
            self.save(update_fields=['gq_deck', 'gq_index'])
        return self.gq_deck[self.gq_index]

    def gold_rush_tick(self):
        """Clear an expired wrong-answer lockout and move to the next question.

        Runs on every state read, so a player self-heals to their next question
        a second after the 3s penalty ends without needing a button.
        """
        if self.gq_locked_until and timezone.now() >= self.gq_locked_until:
            self.gq_locked_until = None
            self.gq_pending = None
            self.gq_index += 1
            self.save(update_fields=['gq_locked_until', 'gq_pending', 'gq_index'])
