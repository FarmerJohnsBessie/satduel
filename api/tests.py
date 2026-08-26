from unittest.mock import patch
from datetime import date, timedelta
from importlib import import_module
from types import SimpleNamespace

from django.contrib.auth.models import User
from django.core import mail
from django.db import connection
from django.test import override_settings, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase, APIClient

from allauth.account.models import EmailAddress
from api import generation
from api.models import (
    Announcement, DirectMessage, DuelEmote, FriendRequest, PendingRegistration, PracticeAttempt, PracticeTestModule,
    Profile, Question, QuestionReport, Room, SATExamDate, SavedQuestion, TrackedQuestion,
)
from api.views.auth_views import PendingRegistrationSerializer
from api.views.serializers import QuestionSerializer


class FriendManagementTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='x')
        self.bob = User.objects.create_user(username='bob', password='x')
        self.carol = User.objects.create_user(username='carol', password='x')
        for user in (self.alice, self.bob, self.carol):
            Profile.objects.create(user=user)
        self.client.force_authenticate(user=self.alice)

    def test_all_requests_returns_incoming_and_outgoing(self):
        FriendRequest.objects.create(from_user=self.bob, to_user=self.alice)
        FriendRequest.objects.create(from_user=self.alice, to_user=self.carol)

        response = self.client.get(reverse('list_friend_requests'), {'scope': 'all'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['incoming'][0]['from_user']['username'], 'bob')
        self.assertEqual(response.data['outgoing'][0]['to_user']['username'], 'carol')

    def test_cancel_only_deletes_own_outgoing_request(self):
        own_request = FriendRequest.objects.create(from_user=self.alice, to_user=self.bob)
        someone_elses = FriendRequest.objects.create(from_user=self.bob, to_user=self.carol)

        self.assertEqual(
            self.client.post(reverse('cancel_friend_request', args=[own_request.id])).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(reverse('cancel_friend_request', args=[someone_elses.id])).status_code,
            404,
        )
        self.assertFalse(FriendRequest.objects.filter(id=own_request.id).exists())
        self.assertTrue(FriendRequest.objects.filter(id=someone_elses.id).exists())

    def test_remove_friend_deletes_chat_history(self):
        self.alice.profile.friends.add(self.bob)
        self.bob.profile.friends.add(self.alice)
        DirectMessage.objects.create(sender=self.alice, recipient=self.bob, content='hello')
        DirectMessage.objects.create(sender=self.bob, recipient=self.alice, content='hi')
        DirectMessage.objects.create(sender=self.carol, recipient=self.alice, content='keep me')

        response = self.client.post(reverse('remove_friend'), {'friend_id': self.bob.id}, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertFalse(DirectMessage.objects.filter(sender=self.alice, recipient=self.bob).exists())
        self.assertFalse(DirectMessage.objects.filter(sender=self.bob, recipient=self.alice).exists())
        self.assertTrue(DirectMessage.objects.filter(sender=self.carol, recipient=self.alice).exists())

    def test_notification_count_includes_messages_and_requests(self):
        DirectMessage.objects.create(sender=self.bob, recipient=self.alice, content='hello')
        FriendRequest.objects.create(from_user=self.carol, to_user=self.alice)

        response = self.client.get(reverse('messages_unread_count'))

        self.assertEqual(response.data, {'unread_count': 1, 'friend_request_count': 1})

    def test_only_recipient_can_respond_to_request(self):
        request = FriendRequest.objects.create(from_user=self.bob, to_user=self.carol)

        response = self.client.post(
            reverse('respond_friend_request', args=[request.id]),
            {'status': 'accepted'},
            format='json',
        )

        self.assertEqual(response.status_code, 404)
        request.refresh_from_db()
        self.assertEqual(request.status, 'pending')


class PasswordLoginTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='alice', email='alice@example.com', password='Secret123',
        )
        Profile.objects.create(user=self.user)

    def test_login_requires_verified_email(self):
        resp = self.client.post(reverse('auth_login'), {
            'username': 'alice', 'password': 'Secret123',
        }, format='json')
        self.assertEqual(resp.status_code, 401)
        self.assertIn('verify', resp.data['error'].lower())

    def test_login_success_returns_tokens_and_user(self):
        EmailAddress.objects.create(
            user=self.user, email=self.user.email, verified=True, primary=True,
        )
        resp = self.client.post(reverse('auth_login'), {
            'username': 'alice', 'password': 'Secret123',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('access', resp.data)
        self.assertIn('refresh', resp.data)
        self.assertEqual(resp.data['user']['username'], 'alice')
        self.assertTrue(resp.data['user']['is_first_login'])

    def test_login_by_email(self):
        EmailAddress.objects.create(
            user=self.user, email=self.user.email, verified=True, primary=True,
        )
        resp = self.client.post(reverse('auth_login'), {
            'username': 'alice@example.com', 'password': 'Secret123',
        }, format='json')
        self.assertEqual(resp.status_code, 200)

    def test_login_bad_password(self):
        EmailAddress.objects.create(
            user=self.user, email=self.user.email, verified=True, primary=True,
        )
        resp = self.client.post(reverse('auth_login'), {
            'username': 'alice', 'password': 'wrong',
        }, format='json')
        self.assertEqual(resp.status_code, 401)

    def test_legacy_token_endpoint_requires_verified_email(self):
        resp = self.client.post(reverse('token_obtain_pair'), {
            'username': 'alice', 'password': 'Secret123',
        }, format='json')
        self.assertEqual(resp.status_code, 401)

    def test_refresh_token_requires_verified_email(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        response = self.client.post(reverse('api_token_refresh'), {
            'refresh': str(RefreshToken.for_user(self.user)),
        }, format='json')
        self.assertEqual(response.status_code, 401)

    def test_verified_user_can_refresh_token(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        EmailAddress.objects.create(user=self.user, email=self.user.email, verified=True, primary=True)
        response = self.client.post(reverse('api_token_refresh'), {
            'refresh': str(RefreshToken.for_user(self.user)),
        }, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('access', response.data)

    def test_duplicate_auth_login_surface_is_not_exposed(self):
        response = self.client.post('/auth/login/', {
            'email': 'alice@example.com', 'password': 'Secret123',
        }, format='json')
        self.assertEqual(response.status_code, 404)


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    FRONTEND_URL='https://satduel.test',
)
class PendingRegistrationTests(APITestCase):
    def payload(self, **updates):
        data = {
            'email': 'new.student@example.com',
            'grade': '10',
            'password1': 'StrongPass123',
            'password2': 'StrongPass123',
            'terms_accepted': True,
            'next_path': '/trainer?subject=math',
        }
        data.update(updates)
        return data

    def register(self):
        return self.client.post(reverse('register'), self.payload(), format='json')

    def verification_key(self):
        marker = '/confirm-email/'
        line = next(line for line in mail.outbox[-1].body.splitlines() if marker in line)
        return line.split(marker, 1)[1].rstrip('/')

    def test_registration_creates_only_pending_credentials(self):
        response = self.register()

        self.assertEqual(response.status_code, 201)
        self.assertTrue(PendingRegistration.objects.filter(email='new.student@example.com').exists())
        self.assertEqual(PendingRegistration.objects.get(email='new.student@example.com').grade, '10')
        self.assertFalse(User.objects.filter(email='new.student@example.com').exists())
        self.assertFalse(Profile.objects.filter(user__email='new.student@example.com').exists())
        self.assertEqual(len(mail.outbox), 1)

    def test_verification_creates_account_profile_and_session(self):
        self.register()
        response = self.client.post(reverse('auth_verify_registration'), {
            'key': self.verification_key(),
        }, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertIn('access', response.data)
        self.assertEqual(response.data['user']['next_path'], '/trainer?subject=math')
        user = User.objects.get(email='new.student@example.com')
        self.assertTrue(EmailAddress.objects.filter(user=user, verified=True).exists())
        self.assertTrue(user.profile.onboarding_required)
        self.assertEqual(user.profile.grade, '10')
        self.assertFalse(user.profile.username_finalized)
        self.assertTrue(user.profile.grade_selected)
        self.assertFalse(PendingRegistration.objects.exists())

    def test_pending_login_explains_that_verification_is_required(self):
        self.register()
        response = self.client.post(reverse('auth_login'), {
            'username': 'new.student@example.com',
            'password': 'StrongPass123',
        }, format='json')

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['code'], 'email_not_verified')

    def test_repeated_registration_is_rate_limited_per_email(self):
        self.register()
        response = self.register()
        self.assertEqual(response.status_code, 429)

    def test_stock_registration_endpoint_is_not_exposed(self):
        response = self.client.post('/auth/registration/', self.payload(), format='json')
        self.assertEqual(response.status_code, 404)

    def test_registration_rejects_grade_below_eight(self):
        serializer = PendingRegistrationSerializer(data=self.payload(grade='7'))

        self.assertFalse(serializer.is_valid())
        self.assertIn('grade', serializer.errors)


class QuestionFilterSubjectTests(APITestCase):
    def test_any_type_can_filter_math_subject(self):
        Question.objects.create(
            question='English?', choice_a='a', choice_b='b', choice_c='c', choice_d='d',
            answer='A', difficulty=1, question_type='Words in Context',
        )
        math = Question.objects.create(
            question='Math?', choice_a='a', choice_b='b', choice_c='c', choice_d='d',
            answer='A', difficulty=1, question_type='Linear functions',
        )

        resp = self.client.get(reverse('list_questions'), {'subject': 'math', 'type': 'any'})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual([question['id'] for question in resp.data['questions']], [math.id])


class QuestionReportTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='reporter', password='Secret123')
        self.admin = User.objects.create_user(username='staff', password='Secret123', is_staff=True)
        self.question = Question.objects.create(
            question='Which choice is correct?',
            choice_a='First', choice_b='Second', choice_c='Third', choice_d='Fourth',
            answer='B', difficulty=1, question_type='Words in Context',
            explanation='The second choice is correct.',
        )

    def test_report_requires_twenty_characters(self):
        self.client.force_authenticate(user=self.user)
        too_short = self.client.post(reverse('create_question_report'), {
            'question_id': self.question.id,
            'reason': 'bad_explanation',
            'details': 'Too short',
        }, format='json')
        accepted = self.client.post(reverse('create_question_report'), {
            'question_id': self.question.id,
            'reason': 'bad_explanation',
            'details': 'The explanation skips an important reasoning step.',
        }, format='json')

        self.assertEqual(too_short.status_code, 400)
        self.assertEqual(accepted.status_code, 201)
        self.assertEqual(QuestionReport.objects.get().reporter, self.user)

    def test_staff_can_review_and_delete_report(self):
        report = QuestionReport.objects.create(
            question=self.question,
            reporter=self.user,
            reason='bad_explanation',
            details='The explanation skips the key reasoning step.',
        )
        self.client.force_authenticate(user=self.admin)

        listed = self.client.get(reverse('list_question_reports'))
        deleted = self.client.delete(reverse('delete_question_report', args=[report.id]))

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.data[0]['question']['explanation'], self.question.explanation)
        self.assertEqual(deleted.status_code, 204)
        self.assertFalse(QuestionReport.objects.exists())


class AnnouncementTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='student', password='Secret123')
        self.admin = User.objects.create_user(username='staff', password='Secret123', is_staff=True)

    def test_active_announcement_is_visible_to_signed_in_users(self):
        Announcement.objects.create(message='New practice test 🎉', is_active=True)
        self.client.force_authenticate(user=self.user)

        response = self.client.get(reverse('active_announcement'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['message'], 'New practice test 🎉')
        self.assertTrue(response.data['version'])

    def test_inactive_announcement_is_hidden(self):
        Announcement.objects.create(message='Not yet', is_active=False)
        self.client.force_authenticate(user=self.user)

        response = self.client.get(reverse('active_announcement'))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data)

    def test_only_staff_can_update_announcement(self):
        self.client.force_authenticate(user=self.user)
        denied = self.client.put(reverse('manage_announcement'), {
            'message': 'Nope', 'is_active': True,
        }, format='json')

        self.client.force_authenticate(user=self.admin)
        allowed = self.client.put(reverse('manage_announcement'), {
            'message': 'Try the new test 🚀', 'is_active': True,
        }, format='json')

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(Announcement.objects.get(pk=1).message, 'Try the new test 🚀')

    def test_active_announcement_requires_a_message(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.put(reverse('manage_announcement'), {
            'message': '   ', 'is_active': True,
        }, format='json')

        self.assertEqual(response.status_code, 400)

    def test_saving_without_changes_keeps_the_same_version(self):
        self.client.force_authenticate(user=self.admin)
        payload = {'message': 'Practice Test 2 is live', 'is_active': True}

        first = self.client.put(reverse('manage_announcement'), payload, format='json')
        second = self.client.put(reverse('manage_announcement'), payload, format='json')

        self.assertEqual(first.data['version'], second.data['version'])


class AdminQuestionDeleteTests(APITestCase):
    """The admin editor's delete button drops the question for good, so the
    endpoint must stay staff-only and take its cascading rows with it."""

    def setUp(self):
        self.user = User.objects.create_user(username='student', password='Secret123')
        self.admin = User.objects.create_user(username='staff', password='Secret123', is_staff=True)
        self.question = Question.objects.create(
            question='Which choice is correct?',
            choice_a='First', choice_b='Second', choice_c='Third', choice_d='Fourth',
            answer='B', difficulty=1, question_type='Words in Context',
            explanation='The second choice is correct.',
        )

    def test_non_staff_cannot_delete_a_question(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.delete(reverse('delete_question', args=[self.question.id]))

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Question.objects.filter(id=self.question.id).exists())

    def test_staff_delete_removes_the_question_and_its_related_rows(self):
        PracticeAttempt.objects.create(
            user=self.user, question=self.question, subject='english', correct=True,
        )
        SavedQuestion.objects.create(user=self.user, question=self.question, subject='english')
        QuestionReport.objects.create(
            question=self.question,
            reporter=self.user,
            reason='bad_explanation',
            details='The explanation skips the key reasoning step.',
        )
        self.client.force_authenticate(user=self.admin)

        response = self.client.delete(reverse('delete_question', args=[self.question.id]))

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Question.objects.filter(id=self.question.id).exists())
        self.assertFalse(PracticeAttempt.objects.exists())
        self.assertFalse(SavedQuestion.objects.exists())
        self.assertFalse(QuestionReport.objects.exists())

    def test_deleting_a_missing_question_is_a_404(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.delete(reverse('delete_question', args=[self.question.id + 999]))

        self.assertEqual(response.status_code, 404)


class QuestionSubjectFieldTests(APITestCase):
    """The serialized `subject` drives the in-card Desmos button, which must
    appear on math questions only."""

    def _serialized_subject(self, question_type):
        question = Question.objects.create(
            question='Q?', choice_a='a', choice_b='b', choice_c='c', choice_d='d',
            answer='A', difficulty=1, question_type=question_type,
        )
        return QuestionSerializer(question).data['subject']

    def test_math_question_type_serializes_as_math(self):
        self.assertEqual(self._serialized_subject('Linear functions'), 'math')
        self.assertEqual(self._serialized_subject('Circles'), 'math')

    def test_english_question_type_serializes_as_english(self):
        self.assertEqual(self._serialized_subject('Words in Context'), 'english')

    def test_unknown_question_type_falls_back_to_english(self):
        self.assertEqual(self._serialized_subject('Not A Real Skill'), 'english')
        self.assertEqual(self._serialized_subject(None), 'english')

    def test_every_math_skill_in_the_taxonomy_is_math(self):
        for skill in generation.MATH_SKILL_NAMES:
            self.assertEqual(generation.subject_of_type(skill), 'math', skill)

    def test_math_and_english_taxonomies_do_not_overlap(self):
        self.assertEqual(generation.MATH_SKILL_NAMES & generation.ENGLISH_SKILL_NAMES, set())


class QuestionMetadataTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='question-admin', is_staff=True)
        self.client.force_authenticate(user=self.admin)

    @staticmethod
    def draft(question_type='One-Variable Data: Distributions and Measures of Center and Spread'):
        return {
            'question': 'What is the answer?',
            'choice_a': '1',
            'choice_b': '2',
            'choice_c': '3',
            'choice_d': '4',
            'answer': 'B',
            'difficulty': 2,
            'question_type': question_type,
            'explanation': 'The answer is 2.',
        }

    def test_question_type_case_is_normalized_on_save(self):
        question = Question.objects.create(**self.draft())

        self.assertEqual(
            question.question_type,
            'One-variable data: distributions and measures of center and spread',
        )
        self.assertEqual(generation.subject_of_type(question.question_type), 'math')

    def test_generator_import_applies_one_source_to_the_batch(self):
        response = self.client.post(reverse('generation_import'), {
            'questions': [self.draft(), self.draft('Two-Variable Data: Models and Scatterplots')],
            'source': Question.SOURCE_OTHER,
            'source_other': 'Teacher-authored set',
        }, format='json')

        self.assertEqual(response.status_code, 200)
        questions = Question.objects.filter(id__in=response.data['created_ids'])
        self.assertEqual(questions.count(), 2)
        self.assertTrue(all(q.source == Question.SOURCE_OTHER for q in questions))
        self.assertTrue(all(q.source_other == 'Teacher-authored set' for q in questions))

    def test_other_source_requires_description(self):
        response = self.client.post(reverse('generation_import'), {
            'questions': [self.draft()],
            'source': Question.SOURCE_OTHER,
        }, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error'], 'Describe the other question source')

    def test_bulk_update_changes_one_shared_field(self):
        questions = [
            Question.objects.create(
                **self.draft('Transitions'), source=Question.SOURCE_SAT_QUESTION_BANK,
            )
            for _ in range(2)
        ]

        response = self.client.post(reverse('bulk_update_questions'), {
            'question_ids': [question.id for question in questions],
            'field': 'source',
            'value': Question.SOURCE_AI_GENERATED,
        }, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['updated'], 2)
        self.assertFalse(Question.objects.filter(id__in=[q.id for q in questions]).exclude(
            source=Question.SOURCE_AI_GENERATED,
        ).exists())

    def test_bulk_update_rejects_mixed_current_values(self):
        sat = Question.objects.create(
            **self.draft('Transitions'), source=Question.SOURCE_SAT_QUESTION_BANK,
        )
        ai = Question.objects.create(
            **self.draft('Transitions'), source=Question.SOURCE_AI_GENERATED,
        )

        response = self.client.post(reverse('bulk_update_questions'), {
            'question_ids': [sat.id, ai.id],
            'field': 'source',
            'value': Question.SOURCE_OTHER,
            'source_other': 'Teacher-authored set',
        }, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('same current source', response.data['error'])


class PracticeTestGenerationTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='test-module-admin', is_staff=True)
        self.client.force_authenticate(user=self.admin)

    @staticmethod
    def math_module_questions():
        types = (
            ['Linear functions'] * 7
            + ['Nonlinear functions'] * 8
            + ['Percentages'] * 4
            + ['Circles'] * 3
        )
        difficulties = [1] * 3 + [2] * 4 + [3] * 7 + [4] * 6 + [5] * 2
        spr_positions = {4, 8, 12, 16}
        mc_index = 0
        questions = []
        for order, (question_type, difficulty) in enumerate(zip(types, difficulties), start=1):
            student_produced = order in spr_positions
            answer = '2' if student_produced else 'ABCD'[mc_index % 4]
            if not student_produced:
                mc_index += 1
            questions.append({
                'order': order,
                'response_type': 'student_produced' if student_produced else 'multiple_choice',
                'question': f'Original question {order}?',
                'choice_a': '' if student_produced else '1',
                'choice_b': '' if student_produced else '2',
                'choice_c': '' if student_produced else '3',
                'choice_d': '' if student_produced else '4',
                'answer': answer,
                'difficulty': difficulty,
                'question_type': question_type,
                'explanation': 'A complete worked explanation.',
            })
        return questions

    @staticmethod
    def english_module_questions():
        types = (
            ['Words in Context'] * 3
            + ['Text Structure and Purpose'] * 2
            + ['Cross-Text Connections'] * 2
            + ['Central Ideas and Details'] * 2
            + ['Command of Evidence'] * 2
            + ['Inferences'] * 3
            + ['Boundaries'] * 4
            + ['Form, Structure, and Sense'] * 3
            + ['Transitions'] * 3
            + ['Rhetorical Synthesis'] * 3
        )
        questions = []
        for order, question_type in enumerate(types, start=1):
            questions.append({
                'order': order,
                'response_type': 'multiple_choice',
                'question': f'Original English question {order}?',
                'choice_a': 'First choice',
                'choice_b': 'Second choice',
                'choice_c': 'Third choice',
                'choice_d': 'Fourth choice',
                'answer': 'ABCD'[(order - 1) % 4],
                'difficulty': 1 + ((order - 1) % 5),
                'question_type': question_type,
                'explanation': 'A complete text-based explanation.',
            })
        return questions

    def test_builds_manual_full_module_prompt(self):
        response = self.client.post(reverse('practice_test_generation_prompt'), {
            'subject': 'math', 'route': 'C',
        }, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertIn('exactly 22', response.data['prompt'])
        self.assertIn('4-6 student-produced', response.data['prompt'])
        self.assertIn('5 characters for a positive answer', response.data['prompt'])
        self.assertIn('separated by semicolons', response.data['prompt'])
        self.assertIn('accepts .6666, .6667, 0.666, or 0.667', response.data['prompt'])
        self.assertIn('Module 2 (higher-difficulty route)', response.data['prompt'])
        self.assertIn('questions 1-15 must each be difficulty 3 or 4', response.data['prompt'])
        self.assertIn('reserve difficulty 5 for the final four', response.data['prompt'])
        self.assertIn('Every question counts toward the score', response.data['prompt'])
        self.assertIn('Never invent a source, citation, or quotation', response.data['prompt'])
        self.assertNotIn('is_pretest', response.data['prompt'])
        self.assertNotIn('ANTHROPIC_API_KEY', response.data['prompt'])

    def test_reading_prompt_enforces_notes_questions_last(self):
        response = self.client.post(reverse('practice_test_generation_prompt'), {
            'subject': 'english', 'route': 'A',
        }, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertIn('Transitions followed by 2-3 Rhetorical', response.data['prompt'])
        self.assertIn('MUST occupy the final positions', response.data['prompt'])
        self.assertIn('keep the average at or below 3.0', response.data['prompt'])
        self.assertIn('grammar point may appear more than twice', response.data['prompt'])
        self.assertIn(
            '<source attribution line>\\n\\n<excerpt text>\\n\\n<question sentence>',
            response.data['prompt'],
        )

    def test_reading_module_two_prompt_hardens_graph_evidence(self):
        response = self.client.post(reverse('practice_test_generation_prompt'), {
            'subject': 'english', 'route': 'C',
        }, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertIn('uses a graph must be difficulty 3, 4, or 5', response.data['prompt'])
        self.assertIn('connect an exact visual trend or comparison', response.data['prompt'])

    def test_imports_hard_math_module_with_final_difficulty_spike(self):
        questions = self.math_module_questions()
        difficulties = [3] * 7 + [4] * 11 + [5] * 4
        for question, difficulty in zip(questions, difficulties):
            question['difficulty'] = difficulty

        response = self.client.post(reverse('practice_test_generation_modules'), {
            'name': 'Math Route C - Calibrated',
            'subject': 'math',
            'route': 'C',
            'questions': questions,
        }, format='json')

        self.assertEqual(response.status_code, 201, response.data)

    def test_rejects_hard_math_module_with_easy_first_fifteen(self):
        questions = self.math_module_questions()
        difficulties = [2] + [3] * 6 + [4] * 11 + [5] * 4
        for question, difficulty in zip(questions, difficulties):
            question['difficulty'] = difficulty

        response = self.client.post(reverse('practice_test_generation_modules'), {
            'name': 'Math Route C - Too easy early',
            'subject': 'math',
            'route': 'C',
            'questions': questions,
        }, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('questions 1-15 must be difficulty 3-4', response.data['error'])

    def test_regular_reading_prompt_enforces_source_spacing(self):
        prompt = generation.build_prompt('Words in Context', 3, 2)

        self.assertIn(
            '<source attribution line>\\n\\n<excerpt text>\\n\\n<question sentence>',
            prompt,
        )
        self.assertIn('The blank line between them is required', prompt)

    def test_imports_module_without_adding_normal_questions(self):
        normal_count = Question.objects.count()
        response = self.client.post(reverse('practice_test_generation_modules'), {
            'name': 'Math Route A - Form 1',
            'subject': 'math',
            'route': 'A',
            'questions': self.math_module_questions(),
        }, format='json')

        self.assertEqual(response.status_code, 201, response.data)
        module = PracticeTestModule.objects.get()
        self.assertEqual(module.question_count, 22)
        self.assertEqual(module.questions[0]['order'], 1)
        self.assertEqual(Question.objects.count(), normal_count)

    def test_rejects_invalid_student_response_answer_format(self):
        questions = self.math_module_questions()
        questions[3]['answer'] = '3 1/2'

        response = self.client.post(reverse('practice_test_generation_modules'), {
            'name': 'Math Route A - Invalid response',
            'subject': 'math',
            'route': 'A',
            'questions': questions,
        }, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('valid Bluebook numeric entry formats', response.data['error'])

    def test_imports_reading_module_in_official_domain_order(self):
        response = self.client.post(reverse('practice_test_generation_modules'), {
            'name': 'Reading Route A - Form 1',
            'subject': 'english',
            'route': 'A',
            'questions': self.english_module_questions(),
        }, format='json')

        self.assertEqual(response.status_code, 201, response.data)
        module = PracticeTestModule.objects.get()
        self.assertEqual(module.questions[0]['question_type'], 'Words in Context')
        self.assertEqual(module.question_count, 27)

    def test_rejects_reading_module_one_above_average_three(self):
        questions = self.english_module_questions()
        raised = 0
        for question in questions:
            if question['difficulty'] == 1 and raised < 2:
                question['difficulty'] = 3
                raised += 1

        response = self.client.post(reverse('practice_test_generation_modules'), {
            'name': 'Reading Route A - Too hard',
            'subject': 'english',
            'route': 'A',
            'questions': questions,
        }, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('average difficulty must be 2.5-3.0', response.data['error'])

    def test_rejects_reading_module_with_notes_before_transitions(self):
        questions = self.english_module_questions()
        questions[-6:-3], questions[-3:] = questions[-3:], questions[-6:-3]
        response = self.client.post(reverse('practice_test_generation_modules'), {
            'name': 'Reading Route A - Wrong Order',
            'subject': 'english',
            'route': 'A',
            'questions': questions,
        }, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('Transitions before final Rhetorical Synthesis', response.data['error'])

    def test_admin_can_view_saved_module_questions(self):
        module = PracticeTestModule.objects.create(
            name='Preview module',
            subject='math',
            route='A',
            questions=self.math_module_questions(),
            created_by=self.admin,
        )

        response = self.client.get(reverse('practice_test_generation_module_detail', args=[module.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['module']['name'], 'Preview module')
        self.assertEqual(len(response.data['module']['questions']), 22)

    def test_rejects_incomplete_module(self):
        response = self.client.post(reverse('practice_test_generation_modules'), {
            'name': 'Incomplete module',
            'subject': 'math',
            'route': 'A',
            'questions': self.math_module_questions()[:-1],
        }, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('exactly 22', response.data['error'])


def _fake_idinfo(email='bob@example.com', verified=True, sub='google-uid-123'):
    return {
        'email': email,
        'email_verified': verified,
        'given_name': 'Bob',
        'family_name': 'Jones',
        'sub': sub,
    }


class GoogleLoginTests(APITestCase):
    @patch('api.views.auth_views.google_id_token.verify_oauth2_token')
    def test_new_google_user_is_created(self, mock_verify):
        mock_verify.return_value = _fake_idinfo()
        resp = self.client.post(reverse('auth_google'), {
            'credential': 'fake-token',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['user']['is_new_user'])
        user = User.objects.get(email='bob@example.com')
        self.assertTrue(Profile.objects.filter(user=user).exists())
        self.assertTrue(EmailAddress.objects.filter(user=user, verified=True).exists())
        self.assertFalse(user.has_usable_password())
        self.assertTrue(resp.data['user']['onboarding_required'])

    @patch('api.views.auth_views.google_id_token.verify_oauth2_token')
    def test_new_google_user_can_choose_username_during_setup(self, mock_verify):
        mock_verify.return_value = _fake_idinfo()
        self.client.post(reverse('auth_google'), {'credential': 'fake-token'}, format='json')
        user = User.objects.get(email='bob@example.com')
        self.client.force_authenticate(user=user)

        response = self.client.post(reverse('auth_complete_profile'), {
            'username': 'sat_champion',
            'grade': '11',
            'sat_exam_date': None,
            'marketing_opt_in': False,
            'terms_accepted': True,
        }, format='json')

        user.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(user.username, 'sat_champion')
        self.assertFalse(response.data['onboarding_required'])

    @patch('api.views.auth_views.google_id_token.verify_oauth2_token')
    def test_google_records_social_account(self, mock_verify):
        from allauth.socialaccount.models import SocialAccount
        mock_verify.return_value = _fake_idinfo(sub='uid-abc')
        self.client.post(reverse('auth_google'), {'credential': 'fake'}, format='json')
        sa = SocialAccount.objects.get(provider='google', uid='uid-abc')
        self.assertEqual(sa.user.email, 'bob@example.com')

    @patch('api.views.auth_views.google_id_token.verify_oauth2_token')
    def test_google_repeat_login_no_duplicate_social(self, mock_verify):
        from allauth.socialaccount.models import SocialAccount
        mock_verify.return_value = _fake_idinfo(sub='uid-xyz')
        self.client.post(reverse('auth_google'), {'credential': 'fake'}, format='json')
        self.client.post(reverse('auth_google'), {'credential': 'fake'}, format='json')
        self.assertEqual(SocialAccount.objects.filter(uid='uid-xyz').count(), 1)

    @patch('api.views.auth_views.google_id_token.verify_oauth2_token')
    def test_google_links_to_existing_verified_account(self, mock_verify):
        existing = User.objects.create_user(
            username='bob', email='bob@example.com', password='Secret123',
        )
        Profile.objects.create(user=existing)
        EmailAddress.objects.create(
            user=existing, email='bob@example.com', verified=True, primary=True,
        )
        mock_verify.return_value = _fake_idinfo()
        resp = self.client.post(reverse('auth_google'), {
            'credential': 'fake-token',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data['user']['is_new_user'])
        self.assertEqual(resp.data['user']['id'], existing.id)
        self.assertEqual(User.objects.filter(email='bob@example.com').count(), 1)

    @patch('api.views.auth_views.google_id_token.verify_oauth2_token')
    def test_google_rejects_unverified_email(self, mock_verify):
        mock_verify.return_value = _fake_idinfo(verified=False)
        resp = self.client.post(reverse('auth_google'), {
            'credential': 'fake-token',
        }, format='json')
        self.assertEqual(resp.status_code, 401)

    @patch('api.views.auth_views.google_id_token.verify_oauth2_token')
    def test_google_invalid_token(self, mock_verify):
        mock_verify.side_effect = ValueError('bad token')
        resp = self.client.post(reverse('auth_google'), {
            'credential': 'fake-token',
        }, format='json')
        self.assertEqual(resp.status_code, 401)

    def test_google_missing_credential(self):
        resp = self.client.post(reverse('auth_google'), {}, format='json')
        self.assertEqual(resp.status_code, 400)

    @patch('api.views.auth_views.google_id_token.verify_oauth2_token')
    def test_google_duplicate_emails_picks_verified(self, mock_verify):
        # Two legacy accounts share an email; only one is verified.
        u1 = User.objects.create_user(username='dup1', email='dup@example.com')
        u2 = User.objects.create_user(username='dup2', email='dup@example.com')
        Profile.objects.create(user=u1)
        Profile.objects.create(user=u2)
        EmailAddress.objects.create(user=u2, email='dup@example.com', verified=True, primary=True)
        mock_verify.return_value = _fake_idinfo(email='dup@example.com')
        resp = self.client.post(reverse('auth_google'), {
            'credential': 'fake-token',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['user']['id'], u2.id)


class CompleteProfileTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='carol', email='c@example.com')
        Profile.objects.create(user=self.user)
        self.exam_date = timezone.localdate() + timedelta(days=30)
        SATExamDate.objects.update_or_create(date=self.exam_date, defaults={'active': True})
        EmailAddress.objects.create(user=self.user, email='c@example.com', verified=True, primary=True)
        self.client.force_authenticate(user=self.user)

    def payload(self, **updates):
        data = {
            'grade': '10',
            'sat_exam_date': self.exam_date.isoformat(),
            'marketing_opt_in': True,
            'terms_accepted': True,
        }
        data.update(updates)
        return data

    def test_complete_account_setup(self):
        resp = self.client.post(reverse('auth_complete_profile'), self.payload(), format='json')
        self.assertEqual(resp.status_code, 200)
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.grade, '10')
        self.assertEqual(self.user.profile.sat_exam_date, self.exam_date)
        self.assertTrue(self.user.profile.sat_exam_date_selected)
        self.assertTrue(self.user.profile.marketing_opt_in)
        self.assertIsNotNone(self.user.profile.terms_accepted_at)

    def test_new_account_chooses_identity_only_after_verification(self):
        self.user.profile.username_finalized = False
        self.user.profile.grade_selected = False
        self.user.profile.save(update_fields=['username_finalized', 'grade_selected'])

        resp = self.client.post(reverse('auth_complete_profile'), self.payload(
            username='carol_sat',
            first_name='Carol',
            last_name='Ng',
        ), format='json')

        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.username, 'carol_sat')
        self.assertEqual(self.user.first_name, 'Carol')
        self.assertTrue(self.user.profile.username_finalized)
        self.assertTrue(self.user.profile.grade_selected)
        self.assertFalse(self.user.profile.onboarding_required)

    def test_invalid_grade_rejected(self):
        resp = self.client.post(reverse('auth_complete_profile'), self.payload(grade='99'), format='json')
        self.assertEqual(resp.status_code, 400)

    def test_terms_are_required(self):
        resp = self.client.post(reverse('auth_complete_profile'), self.payload(terms_accepted=False), format='json')
        self.assertEqual(resp.status_code, 400)

    def test_unknown_exam_date_is_a_completed_choice(self):
        resp = self.client.post(reverse('auth_complete_profile'), self.payload(sat_exam_date=None), format='json')
        self.assertEqual(resp.status_code, 200)
        self.user.profile.refresh_from_db()
        self.assertIsNone(self.user.profile.sat_exam_date)
        self.assertTrue(self.user.profile.sat_exam_date_selected)

    def test_profile_payload_reports_whether_grade_was_selected(self):
        self.user.profile.grade_selected = False
        self.user.profile.save(update_fields=['grade_selected'])

        resp = self.client.get(reverse('profile'))

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data['onboarding']['grade_selected'])
        self.assertTrue(resp.data['onboarding']['username_finalized'])

    def test_requires_auth(self):
        self.client.force_authenticate(user=None)
        resp = self.client.post(reverse('auth_complete_profile'), self.payload(), format='json')
        self.assertEqual(resp.status_code, 401)


class SATExamDateTests(APITestCase):
    def test_returns_only_the_next_six_active_dates(self):
        SATExamDate.objects.all().delete()
        today = timezone.localdate()
        SATExamDate.objects.create(date=today - timedelta(days=1))
        for days in range(1, 8):
            SATExamDate.objects.create(date=today + timedelta(days=days), active=days != 2)

        response = self.client.get(reverse('sat_exam_dates'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['dates'], [
            (today + timedelta(days=days)).isoformat()
            for days in [1, 3, 4, 5, 6, 7]
        ])


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    FRONTEND_URL='https://satduel.test',
)
class AccountSettingsTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='google_user', email='google@example.com')
        self.profile = Profile.objects.create(user=self.user)
        EmailAddress.objects.create(
            user=self.user, email=self.user.email, verified=True, primary=True,
        )
        self.client.force_authenticate(user=self.user)

    def test_username_change_is_limited_to_once_per_30_days(self):
        first = self.client.patch(
            reverse('account_username'), {'username': 'new_name'}, format='json',
        )
        second = self.client.patch(
            reverse('account_username'), {'username': 'another_name'}, format='json',
        )

        self.user.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(self.user.username, 'new_name')
        self.assertIsNotNone(self.profile.username_changed_at)

    def test_profile_patch_cannot_bypass_account_controls(self):
        response = self.client.patch(reverse('profile'), {
            'user': {
                'username': 'bypass',
                'email': 'changed@example.com',
                'first_name': 'Updated',
            },
        }, format='json')

        self.user.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.user.username, 'google_user')
        self.assertEqual(self.user.email, 'google@example.com')
        self.assertEqual(self.user.first_name, 'Updated')

    def _password_link_path(self):
        marker = '/api/reset/'
        line = next(line for line in mail.outbox[-1].body.splitlines() if marker in line)
        return line.split('satduel.test', 1)[1]

    def test_google_only_user_sets_password_through_emailed_link(self):
        response = self.client.post(reverse('auth_set_password'), {}, format='json')

        self.user.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['email'], self.user.email)
        self.assertFalse(self.user.has_usable_password())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, 'Set your SAT Duel password')
        self.assertEqual(len(mail.outbox[0].alternatives), 1)

        confirm = self.client.post(self._password_link_path(), {
            'new_password1': 'NewSecret123!',
            'new_password2': 'NewSecret123!',
        }, format='json')

        self.user.refresh_from_db()
        self.assertEqual(confirm.status_code, 200)
        self.assertTrue(self.user.check_password('NewSecret123!'))
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(mail.outbox[-1].subject, 'Your SAT Duel password was changed')

    def test_existing_password_is_changed_only_through_emailed_link(self):
        self.user.set_password('ExistingSecret123!')
        self.user.save(update_fields=['password'])

        response = self.client.post(reverse('auth_set_password'), {}, format='json')

        self.user.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.user.check_password('ExistingSecret123!'))
        self.assertEqual(mail.outbox[-1].subject, 'Change your SAT Duel password')


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    FRONTEND_URL='https://satduel.test',
)
class PasswordResetEmailTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='reset_user', email='reset@example.com', password='ExistingSecret123!',
        )
        Profile.objects.create(user=self.user)
        EmailAddress.objects.create(user=self.user, email=self.user.email, verified=True, primary=True)

    def test_reset_request_and_confirmation_use_branded_emails(self):
        request_response = self.client.post(
            reverse('password_reset'), {'email': self.user.email}, format='json',
        )

        self.assertEqual(request_response.status_code, 200)
        self.assertEqual(mail.outbox[0].subject, 'Change your SAT Duel password')
        self.assertIn('SAT Duel', mail.outbox[0].body)
        self.assertEqual(len(mail.outbox[0].alternatives), 1)

        marker = '/api/reset/'
        line = next(line for line in mail.outbox[0].body.splitlines() if marker in line)
        confirm_response = self.client.post(line.split('satduel.test', 1)[1], {
            'new_password1': 'Replacement123!',
            'new_password2': 'Replacement123!',
        }, format='json')

        self.assertEqual(confirm_response.status_code, 200)
        self.assertEqual(mail.outbox[-1].subject, 'Your SAT Duel password was changed')

    def test_reset_request_does_not_reveal_unknown_email(self):
        response = self.client.post(
            reverse('password_reset'), {'email': 'unknown@example.com'}, format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)


class ProfileGradePromotionTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='grade_user', email='grade@example.com')

    def test_grade_advances_once_each_september_and_catches_up(self):
        profile = Profile.objects.create(
            user=self.user,
            grade='8',
            grade_last_promoted_year=2024,
        )

        self.assertTrue(profile.promote_grade_for_school_year(date(2026, 9, 1)))
        self.assertEqual(profile.grade, '10')
        self.assertEqual(profile.grade_last_promoted_year, 2026)
        self.assertFalse(profile.promote_grade_for_school_year(date(2026, 12, 1)))

    def test_grade_caps_above_grade_twelve(self):
        profile = Profile.objects.create(
            user=self.user,
            grade='12',
            grade_last_promoted_year=2025,
        )

        profile.promote_grade_for_school_year(date(2026, 9, 1))

        self.assertEqual(profile.grade, '>12')


class AccountDeletionTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='delete_me', email='delete@example.com')
        self.profile = Profile.objects.create(user=self.user)
        self.client.force_authenticate(user=self.user)

    def test_confirmation_is_required(self):
        response = self.client.delete(
            reverse('account_delete'), {'confirmation': 'delete'}, format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_deletion_removes_user_data_and_submitted_reports(self):
        from api.models import PracticeAttempt

        question = Question.objects.create(
            question='Delete test?', choice_a='a', choice_b='b',
            choice_c='c', choice_d='d', answer='A', difficulty=1,
        )
        PracticeAttempt.objects.create(
            user=self.user, question=question, correct=False, selected_choice='b',
        )
        report = QuestionReport.objects.create(
            question=question,
            reporter=self.user,
            reason='other',
            details='This report belongs to the deleted user.',
        )

        response = self.client.delete(
            reverse('account_delete'), {'confirmation': 'DELETE'}, format='json',
        )

        self.assertEqual(response.status_code, 204)
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
        self.assertFalse(PracticeAttempt.objects.filter(user_id=self.user.pk).exists())
        self.assertFalse(QuestionReport.objects.filter(pk=report.pk).exists())

    @override_settings(
        STRIPE_SECRET_KEY='stripe_secret_mock',
        STRIPE_API_VERSION='2026-06-24.dahlia',
    )
    @patch('api.account_deletion.stripe.Customer.delete')
    def test_deletion_removes_stripe_customer_first(self, mock_customer_delete):
        self.profile.stripe_customer_id = 'cus_delete_me'
        self.profile.save(update_fields=['stripe_customer_id'])

        response = self.client.delete(
            reverse('account_delete'), {'confirmation': 'DELETE'}, format='json',
        )

        self.assertEqual(response.status_code, 204)
        mock_customer_delete.assert_called_once_with('cus_delete_me')
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())

    @override_settings(
        STRIPE_SECRET_KEY='stripe_secret_mock',
        STRIPE_API_VERSION='2026-06-24.dahlia',
    )
    @patch('api.account_deletion.StripeError', Exception)
    @patch('api.account_deletion.stripe.Customer.delete')
    def test_stripe_failure_keeps_local_account(self, mock_customer_delete):
        self.profile.stripe_customer_id = 'cus_delete_me'
        self.profile.save(update_fields=['stripe_customer_id'])
        mock_customer_delete.side_effect = Exception('Stripe unavailable')

        response = self.client.delete(
            reverse('account_delete'), {'confirmation': 'DELETE'}, format='json',
        )

        self.assertEqual(response.status_code, 502)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_refresh_token_for_deleted_account_returns_unauthorized(self):
        from rest_framework_simplejwt.tokens import RefreshToken

        refresh_token = str(RefreshToken.for_user(self.user))
        delete_response = self.client.delete(
            reverse('account_delete'), {'confirmation': 'DELETE'}, format='json',
        )

        refresh_response = self.client.post(
            reverse('api_token_refresh'), {'refresh': refresh_token}, format='json',
        )

        self.assertEqual(delete_response.status_code, 204)
        self.assertEqual(refresh_response.status_code, 401)


class AccountDeletionAdminTests(APITestCase):
    def test_user_delete_permission_includes_cascade_owned_data(self):
        from django.contrib import admin
        from django.contrib.auth.models import Permission
        from django.test import RequestFactory
        from api.admin import UserAdmin

        staff = User.objects.create_user(username='staff', is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename='delete_user'))
        target = User.objects.create_user(username='admin_delete_target')
        Profile.objects.create(user=target)
        request = RequestFactory().get('/admin/auth/user/')
        request.user = staff
        user_admin = UserAdmin(User, admin.site)

        _deleted, _counts, perms_needed, protected = user_admin.get_deleted_objects([target], request)

        self.assertFalse(staff.has_perm('api.delete_profile'))
        self.assertEqual(perms_needed, set())
        self.assertEqual(protected, [])
        user_admin.delete_model(request, target)
        self.assertFalse(User.objects.filter(pk=target.pk).exists())

    def test_bulk_admin_action_deletes_user(self):
        admin_user = User.objects.create_superuser(username='delete_admin')
        target = User.objects.create_user(username='bulk_delete_target')
        Profile.objects.create(user=target)
        self.client.force_login(admin_user)
        url = reverse('admin:auth_user_changelist')
        selection = {
            'action': 'delete_selected',
            '_selected_action': [str(target.pk)],
        }

        response = self.client.post(url, {**selection, 'post': 'yes'})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(User.objects.filter(pk=target.pk).exists())


class OrphanedUserTablesMigrationTests(TransactionTestCase):
    tables = ('api_house', 'oauth2_provider_application')

    def tearDown(self):
        with connection.cursor() as cursor:
            for table in self.tables:
                cursor.execute(f'DROP TABLE IF EXISTS {connection.ops.quote_name(table)}')
        super().tearDown()

    def test_cleanup_drops_legacy_user_foreign_keys(self):
        user = User.objects.create_user(username='legacy_table_user')
        with connection.cursor() as cursor:
            for table in self.tables:
                cursor.execute(
                    f'CREATE TABLE {connection.ops.quote_name(table)} '
                    '(id integer PRIMARY KEY, user_id integer REFERENCES auth_user(id))'
                )
                cursor.execute(
                    f'INSERT INTO {connection.ops.quote_name(table)} (id, user_id) VALUES (1, %s)',
                    [user.pk],
                )

        migration = import_module('api.migrations.0064_drop_orphaned_user_tables')
        with connection.schema_editor() as schema_editor:
            migration.drop_orphaned_user_tables(None, schema_editor)

        remaining_tables = connection.introspection.table_names()
        self.assertNotIn('api_house', remaining_tables)
        self.assertNotIn('oauth2_provider_application', remaining_tables)


class RegistrationAbuseTests(APITestCase):
    def test_same_email_cannot_reserve_multiple_usernames(self):
        User.objects.create_user(username='first_name', email='student@example.com')

        response = self.client.post(reverse('register'), {
            'username': 'second_name',
            'email': 'STUDENT@example.com',
            'first_name': 'Test',
            'last_name': 'Student',
            'password1': 'Secret123!',
            'password2': 'Secret123!',
            'grade': '11',
            'sat_exam_date': None,
            'marketing_opt_in': True,
            'terms_accepted': True,
        }, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(User.objects.filter(email__iexact='student@example.com').count(), 1)


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class RegistrationOnboardingTests(APITestCase):
    def setUp(self):
        self.exam_date = timezone.localdate() + timedelta(days=45)
        SATExamDate.objects.update_or_create(date=self.exam_date, defaults={'active': True})

    def registration_data(self, **updates):
        data = {
            'username': 'new_student',
            'email': 'new_student@example.com',
            'first_name': 'New',
            'last_name': 'Student',
            'password1': 'Secret123!',
            'password2': 'Secret123!',
            'grade': '11',
            'sat_exam_date': self.exam_date.isoformat(),
            'marketing_opt_in': False,
            'terms_accepted': True,
        }
        data.update(updates)
        return data

    def test_registration_defers_identity_and_onboarding_choices(self):
        response = self.client.post(reverse('register'), self.registration_data(), format='json')

        self.assertEqual(response.status_code, 201)
        self.assertTrue(PendingRegistration.objects.filter(email='new_student@example.com').exists())
        self.assertEqual(PendingRegistration.objects.get(email='new_student@example.com').grade, '11')
        self.assertFalse(User.objects.filter(username='new_student').exists())
        self.assertFalse(Profile.objects.filter(user__email='new_student@example.com').exists())

    def test_registration_rejects_unaccepted_terms(self):
        response = self.client.post(
            reverse('register'),
            self.registration_data(terms_accepted=False),
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(username='new_student').exists())


class LeaderboardViewTests(APITestCase):
    def setUp(self):
        from api.models import PracticeStats
        self.users = []
        rows = [
            ('nova', 1700, 1310, 1200, 8),
            ('ember', 1540, 1480, 1740, 15),
            ('cipher', 1810, 1220, 1330, 3),
            ('mira', 1360, 1660, 1410, 6),
        ]
        for username, duel_elo, practice_elo, math_elo, streak in rows:
            user = User.objects.create_user(username=username, email=f'{username}@e.com')
            Profile.objects.create(user=user, elo_rating=duel_elo, max_streak=streak)
            PracticeStats.objects.create(user=user, subject='english', elo=practice_elo)
            PracticeStats.objects.create(user=user, subject='math', elo=math_elo)
            self.users.append(user)
        self.client.force_authenticate(user=self.users[1])

    def test_duel_leaderboard_orders_by_duel_rating(self):
        resp = self.client.get(reverse('leaderboard'), {'metric': 'duel'})
        self.assertEqual(resp.status_code, 200)
        names = [entry['user']['username'] for entry in resp.data['entries']]
        self.assertLess(names.index('cipher'), names.index('nova'))
        self.assertLess(names.index('nova'), names.index('ember'))
        self.assertLess(names.index('ember'), names.index('mira'))
        self.assertTrue(any(Profile.objects.get(user__username=name).is_bot for name in names))
        self.assertEqual(resp.data['current_user']['user']['username'], 'ember')

    def test_practice_leaderboard_orders_by_practice_rating(self):
        resp = self.client.get(reverse('leaderboard'), {'metric': 'practice', 'limit': 2})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([entry['user']['username'] for entry in resp.data['entries']], ['mira', 'ember'])
        self.assertEqual(resp.data['current_user']['rank'], 2)
        self.assertEqual(resp.data['total_users'], 4)

    def test_math_practice_leaderboard_orders_by_math_rating(self):
        resp = self.client.get(reverse('leaderboard'), {'metric': 'practice_math'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['entries'][0]['user']['username'], 'ember')
        self.assertEqual(resp.data['entries'][0]['metric_value'], 1740)

    def test_leaderboard_counts_stored_subject_attempts_for_every_user(self):
        from api.models import PracticeStats
        PracticeStats.objects.filter(user=self.users[0], subject='english').update(answered=1, correct=1)
        PracticeStats.objects.filter(user=self.users[0], subject='math').update(answered=1, correct=0)
        PracticeStats.objects.filter(user=self.users[1], subject='math').update(answered=1, correct=1)

        resp = self.client.get(reverse('leaderboard'), {'metric': 'practice'})

        by_name = {entry['user']['username']: entry for entry in resp.data['entries']}
        self.assertEqual(by_name['nova']['english_answered'], 1)
        self.assertEqual(by_name['nova']['math_answered'], 1)
        self.assertEqual(by_name['nova']['questions_answered'], 2)
        self.assertEqual(by_name['ember']['math_answered'], 1)

    def test_streak_leaderboard_orders_by_max_streak(self):
        resp = self.client.get(reverse('leaderboard'), {'metric': 'streak'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['entries'][0]['user']['username'], 'ember')
        self.assertEqual(resp.data['entries'][0]['metric_value'], 15)

    def test_invalid_metric_is_rejected(self):
        resp = self.client.get(reverse('leaderboard'), {'metric': 'coins'})
        self.assertEqual(resp.status_code, 400)


class DuelEloTests(APITestCase):
    def test_ended_room_only_applies_elo_and_solved_once(self):
        user1 = User.objects.create_user(username='duelist1', email='d1@e.com')
        user2 = User.objects.create_user(username='duelist2', email='d2@e.com')
        profile1 = Profile.objects.create(user=user1, elo_rating=1500)
        profile2 = Profile.objects.create(user=user2, elo_rating=1500)
        question = Question.objects.create(
            question='2 + 2?',
            choice_a='3',
            choice_b='4',
            choice_c='5',
            choice_d='6',
            answer='B',
            difficulty=3,
            question_type='Transitions',
        )
        room = Room.objects.create(
            user1=user1,
            user2=user2,
            status='Battling',
            user1_score=7,
            user2_score=3,
        )
        room.questions.set([question])

        room.status = 'Ended'
        room.save()
        room.refresh_from_db()
        profile1.refresh_from_db()
        profile2.refresh_from_db()
        first_elo1 = profile1.elo_rating
        first_elo2 = profile2.elo_rating
        self.assertEqual((room.user1_elo_before, room.user2_elo_before), (1500, 1500))
        self.assertEqual((room.user1_elo_after, room.user2_elo_after), (first_elo1, first_elo2))

        room.save()
        profile1.refresh_from_db()
        profile2.refresh_from_db()
        self.assertEqual(profile1.elo_rating, first_elo1)
        self.assertEqual(profile2.elo_rating, first_elo2)


class BotDuelTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='human_duelist', email='human@e.com')
        Profile.objects.create(user=self.user, elo_rating=1500)
        self.client.force_authenticate(user=self.user)
        self.questions = [
            Question.objects.create(
                question=f'Duel Q{i}?', choice_a='a', choice_b='b', choice_c='c', choice_d='d',
                answer='B', difficulty=3, question_type='Transitions',
            )
            for i in range(10)
        ]

    def _bot(self):
        return User.objects.filter(profile__is_bot=True).select_related('profile').first()

    def _bot_for_emote_mode(self, mode):
        from api.views.duel_views import _bot_emote_mode
        return next(
            bot for bot in User.objects.filter(profile__is_bot=True).select_related('profile')
            if _bot_emote_mode(bot) == mode
        )

    def _room(self, started_seconds_ago=0):
        room = Room.objects.create(user1=self.user, user2=self._bot(), status='Battling')
        if started_seconds_ago:
            room.battle_start_time = timezone.now() - timedelta(seconds=started_seconds_ago)
            room.save(update_fields=['battle_start_time'])
        return room

    def test_seeded_bots_are_flagged_and_have_reserved_emails(self):
        bots = User.objects.filter(profile__is_bot=True)
        self.assertEqual(bots.count(), 24)
        self.assertTrue(all(user.email.endswith('@bots.satduel.invalid') for user in bots))
        self.assertTrue(all(not user.has_usable_password() for user in bots))
        self.assertEqual(bots.exclude(profile__avatar_icon='initial').count(), 6)
        self.assertEqual(bots.filter(profile__is_premium=True).count(), 2)
        self.assertLessEqual(bots.order_by('-profile__elo_rating').first().profile.elo_rating, 1750)
        self.assertTrue(all(len(user.profile.duel_emotes) == 4 for user in bots.select_related('profile')))

    def test_bot_profiles_are_searchable_without_exposing_email(self):
        response = self.client.get('/api/profile/search/', {'q': 'maddie'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]['username'], 'maddie07')
        self.assertNotIn('email', response.data[0])

    def test_duel_leaderboard_includes_bots_but_practice_does_not(self):
        duel = self.client.get('/api/leaderboard/', {'metric': 'duel'})
        practice = self.client.get('/api/leaderboard/', {'metric': 'practice'})
        bot_names = set(User.objects.filter(profile__is_bot=True).values_list('username', flat=True))
        self.assertTrue(bot_names.intersection(row['user']['username'] for row in duel.data['entries']))
        self.assertFalse(bot_names.intersection(row['user']['username'] for row in practice.data['entries']))

    def test_profile_requires_four_unique_duel_emotes(self):
        invalid = self.client.patch('/api/profile/', {'duel_emotes': ['👍', '👍', '🔥', '😂']}, format='json')
        valid = self.client.patch('/api/profile/', {'duel_emotes': ['🎉', '💀', '👀', '🧠']}, format='json')
        premium_denied = self.client.patch('/api/profile/', {'duel_emotes': ['🤡', '👍', '🔥', '😂']}, format='json')
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(premium_denied.status_code, 400)
        self.assertEqual(valid.data['duel_emotes'], ['🎉', '💀', '👀', '🧠'])

        self.user.profile.is_premium = True
        self.user.profile.save(update_fields=['is_premium'])
        premium_allowed = self.client.patch('/api/profile/', {'duel_emotes': ['🤡', '👎', '🔥', '😂']}, format='json')
        self.assertEqual(premium_allowed.status_code, 200)

    def test_online_list_includes_self_and_hides_bot_metadata(self):
        response = self.client.get('/api/online_users/')
        rows = response.json()['users']
        bot_ids = set(User.objects.filter(profile__is_bot=True).values_list('id', flat=True))
        bot_rows = [user for user in rows if user['id'] in bot_ids]
        self.assertTrue(rows[0]['is_current_user'])
        self.assertEqual(rows[0]['id'], self.user.id)
        self.assertIn(len(bot_rows), (3, 4))
        self.assertTrue(all('avatar_icon' in user for user in bot_rows))
        self.assertTrue(all('is_bot' not in user for user in rows))

    @patch('api.views.duel_views.random.random', return_value=0)
    def test_match_waits_before_bot_fallback(self, _random):
        response = self.client.get('/api/match/')
        room = Room.objects.get(id=response.data['id'])
        self.assertEqual(room.status, 'Searching')
        self.assertIsNone(room.user2)

        waiting = self.client.get('/api/match/status/', {'room_id': room.id})
        self.assertEqual(waiting.data['status'], 'waiting')
        Room.objects.filter(pk=room.pk).update(created_at=timezone.now() - timedelta(seconds=6))

        matched = self.client.get('/api/match/status/', {'room_id': room.id})
        room.refresh_from_db()
        self.assertEqual(matched.data['status'], 'full')
        self.assertTrue(room.user2.profile.is_bot)
        self.assertEqual(TrackedQuestion.objects.filter(room=room, user=self.user).count(), 10)
        self.assertEqual(TrackedQuestion.objects.filter(room=room, user=room.user2).count(), 10)

    def test_match_prioritizes_a_waiting_human(self):
        other = User.objects.create_user(username='waiting_human', email='waiting@e.com')
        Profile.objects.create(user=other, elo_rating=1600)
        room = Room.objects.create(user1=other, status='Searching')

        response = self.client.get('/api/match/')
        room.refresh_from_db()

        self.assertEqual(response.data['full'], 'true')
        self.assertEqual(room.user2_id, self.user.id)

    def test_polling_advances_bot_progress(self):
        room = self._room(started_seconds_ago=100)
        response = self.client.post('/api/match/get_opponent_progress/', {'room_id': room.id}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertGreater(sum(row['status'] != 'Blank' for row in response.data), 0)

    def test_bot_accuracy_uses_current_elo_and_rng(self):
        from api.bot_duels import advance_bot, bot_accuracy
        room = self._room(started_seconds_ago=100)
        bot = room.user2
        bot.profile.elo_rating = 1200
        bot.profile.save(update_fields=['elo_rating'])
        with patch('api.bot_duels.random.randrange', return_value=60):
            advance_bot(room, bot)
        first = TrackedQuestion.objects.filter(room=room, user=bot).order_by('id').first()
        self.assertEqual(first.status, 'Incorrect')

        TrackedQuestion.objects.filter(room=room, user=bot).update(status='Blank')
        bot.profile.elo_rating = 1700
        bot.profile.save(update_fields=['elo_rating'])
        with patch('api.bot_duels.random.randrange', return_value=60):
            advance_bot(room, bot)
        first.refresh_from_db()
        self.assertEqual(first.status, 'Correct')
        self.assertLess(bot_accuracy(1200), bot_accuracy(1700))

    def test_duel_answer_is_graded_server_side(self):
        room = self._room()
        tracked = TrackedQuestion.objects.filter(room=room, user=self.user).order_by('id').first()
        response = self.client.post('/api/match/update/', {
            'tracked_question_id': tracked.id,
            'selected_choice': 'a',
            'result': 'correct',
        }, format='json')
        tracked.refresh_from_db()
        self.assertEqual(response.data['result'], 'incorrect')
        self.assertEqual(tracked.status, 'Incorrect')
        reveal = self.client.post('/api/get_answer/', {'question_id': tracked.question_id}, format='json')
        self.assertEqual(reveal.status_code, 200)

    @patch('api.views.duel_views.random.random', return_value=1)
    @patch('api.views.duel_views.random.choice', return_value='🔥')
    @patch('api.views.duel_views.random.uniform', return_value=0)
    def test_bot_responds_to_emote(self, _uniform, _choice, _random):
        room = Room.objects.create(
            user1=self.user,
            user2=self._bot_for_emote_mode('single'),
            status='Battling',
        )
        response = self.client.post('/api/match/emotes/', {
            'room_id': room.id,
            'emoji': '👍',
        }, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DuelEmote.objects.filter(room=room).count(), 2)
        self.assertEqual({row['emoji'] for row in response.data['emotes']}, {'👍', '🔥'})

    @patch('api.views.duel_views.random.random', return_value=1)
    @patch('api.views.duel_views.random.randint', return_value=3)
    @patch('api.views.duel_views.random.choice', return_value='🔥')
    @patch('api.views.duel_views.random.uniform', return_value=0)
    def test_bot_emote_personalities_include_none_and_spam(self, _uniform, _choice, _randint, _random):
        for mode, expected in [('none', 0), ('spam', 3)]:
            bot = self._bot_for_emote_mode(mode)
            room = Room.objects.create(user1=self.user, user2=bot, status='Battling')
            response = self.client.post('/api/match/emotes/', {
                'room_id': room.id,
                'emoji': '👍',
            }, format='json')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(DuelEmote.objects.filter(room=room, sender=bot).count(), expected)

    def test_end_match_calculates_scores_before_elo(self):
        room = self._room()
        mine = list(TrackedQuestion.objects.filter(room=room, user=self.user).order_by('id'))
        theirs = list(TrackedQuestion.objects.filter(room=room, user=room.user2).order_by('id'))
        for row in mine[:3]:
            row.status = 'Correct'
        for row in mine[3:]:
            row.status = 'Incorrect'
        for row in theirs[:2]:
            row.status = 'Correct'
        for row in theirs[2:]:
            row.status = 'Incorrect'
        TrackedQuestion.objects.bulk_update(mine + theirs, ['status'])

        response = self.client.post('/api/match/end_match/', {'room_id': room.id}, format='json')
        room.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual((room.user1_score, room.user2_score), (3, 2))
        self.assertEqual(room.winner_id, self.user.id)
        self.assertEqual(room.user1_elo_after - room.user1_elo_before, 8)

        result = self.client.post('/api/match/get_results/', {'room_id': room.id}, format='json')
        self.assertEqual(result.data['outcome'], 'win')
        self.assertEqual(result.data['current_user']['elo_change'], 8)
        self.assertNotIn('is_bot', result.data['opponent'])


class CleanupUnverifiedUsersTests(APITestCase):
    def setUp(self):
        from django.utils import timezone
        from datetime import timedelta
        old = timezone.now() - timedelta(days=45)

        # stale: unverified, never logged in, old -> should be deleted
        self.stale = User.objects.create_user(username='stale', email='stale@e.com')
        User.objects.filter(pk=self.stale.pk).update(date_joined=old)

        # recent unverified -> kept
        self.recent = User.objects.create_user(username='recent', email='recent@e.com')

        # verified old -> kept
        self.verified = User.objects.create_user(username='verified', email='v@e.com')
        User.objects.filter(pk=self.verified.pk).update(date_joined=old)
        EmailAddress.objects.create(user=self.verified, email='v@e.com', verified=True, primary=True)

        # staff, unverified, old -> kept (never touch staff)
        self.staff = User.objects.create_user(username='staff', email='s@e.com', is_staff=True)
        User.objects.filter(pk=self.staff.pk).update(date_joined=old)

    def test_dry_run_deletes_nothing(self):
        from django.core.management import call_command
        call_command('cleanup_unverified_users')
        self.assertTrue(User.objects.filter(username='stale').exists())

    def test_delete_only_removes_stale(self):
        from django.core.management import call_command
        call_command('cleanup_unverified_users', '--delete')
        self.assertFalse(User.objects.filter(username='stale').exists())
        self.assertTrue(User.objects.filter(username='recent').exists())
        self.assertTrue(User.objects.filter(username='verified').exists())
        self.assertTrue(User.objects.filter(username='staff').exists())


class PracticeTierTests(APITestCase):
    """Free-vs-premium quota, topic gating, and the revised Elo rules."""

    def setUp(self):
        from api.models import Question
        self.user = User.objects.create_user(username='practicer', email='p@e.com')
        self.profile = Profile.objects.create(user=self.user)
        self.client.force_authenticate(user=self.user)
        self.questions = [
            Question.objects.create(
                question=f'Q{i}?', choice_a='a', choice_b='b', choice_c='c', choice_d='d',
                answer='B', difficulty=3, question_type='Transitions',
            )
            for i in range(5)
        ]

    def _answer(self, q, choice='b', mode='practice'):
        return self.client.post('/api/check_answer/', {
            'question_id': q.id, 'selected_choice': choice, 'mode': mode,
        }, format='json')

    def _stats(self, subject='english', user=None):
        from api.views.practice_views import get_practice_stats
        stats = get_practice_stats(user or self.user, subject)
        stats.refresh_from_db()
        return stats

    def test_next_question_returns_question_and_quota(self):
        resp = self.client.get('/api/practice/next/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('question', resp.data)
        self.assertEqual(resp.data['quota']['limit'], 25)
        self.assertFalse(resp.data['quota']['is_premium'])
        self.assertIn('reset_at', resp.data['quota'])

    def test_quota_uses_the_profile_local_day(self):
        import datetime
        from api.models import PracticeAttempt

        self.profile.timezone = 'America/Los_Angeles'
        self.profile.save(update_fields=['timezone'])
        old = PracticeAttempt.objects.create(
            user=self.user, question=self.questions[0], correct=True,
        )
        current = PracticeAttempt.objects.create(
            user=self.user, question=self.questions[1], correct=True,
        )
        PracticeAttempt.objects.filter(pk=old.pk).update(
            created_at=datetime.datetime(2026, 7, 10, 1, tzinfo=datetime.timezone.utc),
        )
        PracticeAttempt.objects.filter(pk=current.pk).update(
            created_at=datetime.datetime(2026, 7, 10, 23, tzinfo=datetime.timezone.utc),
        )
        now = datetime.datetime(2026, 7, 11, 1, tzinfo=datetime.timezone.utc)

        with patch('api.views.practice_views.timezone.now', return_value=now):
            resp = self.client.get('/api/practice/status/')

        self.assertEqual(resp.data['quota']['used'], 1)
        self.assertEqual(resp.data['quota']['timezone'], 'America/Los_Angeles')
        self.assertEqual(
            resp.data['quota']['reset_at'], '2026-07-11T07:00:00+00:00',
        )

    def test_next_question_reuses_active_until_answered(self):
        first = self.client.get('/api/practice/next/')
        second = self.client.get('/api/practice/next/')
        self.assertEqual(first.data['question']['id'], second.data['question']['id'])

        question = Question.objects.get(id=first.data['question']['id'])
        resp = self._answer(question, 'b')
        self.assertEqual(resp.status_code, 200)
        from api.models import PracticeActiveQuestion
        self.assertFalse(PracticeActiveQuestion.objects.filter(user=self.user).exists())

    def test_practice_must_answer_active_question(self):
        first = self.client.get('/api/practice/next/')
        other = next(q for q in self.questions if q.id != first.data['question']['id'])
        resp = self._answer(other, 'b')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['error'], 'active_question_required')

    def test_topic_selection_requires_premium(self):
        resp = self.client.get('/api/practice/next/', {'type': 'Transitions'})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data['error'], 'premium_required')

    def test_premium_can_select_topic(self):
        self.profile.is_premium = True
        self.profile.save()
        resp = self.client.get('/api/practice/next/', {'type': 'Transitions'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['question']['question_type'], 'Transitions')
        self.assertIsNone(resp.data['quota']['limit'])

    def test_premium_topic_switches_resume_each_unanswered_question(self):
        from api.models import PracticeActiveQuestion

        self.profile.is_premium = True
        self.profile.save()
        Question.objects.create(
            question='Inference?', choice_a='a', choice_b='b', choice_c='c', choice_d='d',
            answer='B', difficulty=3, question_type='Inferences',
        )

        transitions = self.client.get('/api/practice/next/', {'type': 'Transitions'})
        inferences = self.client.get('/api/practice/next/', {'type': 'Inferences'})
        transitions_again = self.client.get('/api/practice/next/', {'type': 'Transitions'})
        inferences_again = self.client.get('/api/practice/next/', {'type': 'Inferences'})

        self.assertNotEqual(transitions.data['question']['id'], inferences.data['question']['id'])
        self.assertEqual(transitions.data['question']['id'], transitions_again.data['question']['id'])
        self.assertEqual(inferences.data['question']['id'], inferences_again.data['question']['id'])
        self.assertEqual(PracticeActiveQuestion.objects.filter(user=self.user).count(), 2)

    def test_multiple_topics_use_the_first_topic_active_question(self):
        from api.models import PracticeActiveQuestion

        self.profile.is_premium = True
        self.profile.save()
        Question.objects.create(
            question='Inference?', choice_a='a', choice_b='b', choice_c='c', choice_d='d',
            answer='B', difficulty=3, question_type='Inferences',
        )

        transitions = self.client.get('/api/practice/next/', {
            'types': 'Transitions,Inferences',
        })
        expanded = self.client.get('/api/practice/next/', {
            'types': 'Transitions,Inferences,Words in Context', 'levels': '5',
        })
        inferences = self.client.get('/api/practice/next/', {
            'types': 'Inferences,Transitions',
        })
        inferences_alone = self.client.get('/api/practice/next/', {
            'types': 'Inferences',
        })

        self.assertEqual(transitions.data['question']['question_type'], 'Transitions')
        self.assertEqual(transitions.data['question']['id'], expanded.data['question']['id'])
        self.assertEqual(inferences.data['question']['question_type'], 'Inferences')
        self.assertEqual(inferences.data['question']['id'], inferences_alone.data['question']['id'])
        self.assertEqual(PracticeActiveQuestion.objects.filter(user=self.user).count(), 2)

    def test_expired_premium_is_free_tier(self):
        from django.utils import timezone
        from datetime import timedelta
        self.profile.is_premium = True
        self.profile.premium_until = timezone.now() - timedelta(days=1)
        self.profile.save()
        resp = self.client.get('/api/practice/next/', {'type': 'Transitions'})
        self.assertEqual(resp.status_code, 403)

    def test_daily_limit_blocks_after_quota(self):
        from api.models import PracticeAttempt, Question
        # Simulate having used the full quota today
        for i in range(25):
            PracticeAttempt.objects.create(
                user=self.user, question=self.questions[i % 5], correct=True,
            )
        next_resp = self.client.get('/api/practice/next/')
        self.assertEqual(next_resp.status_code, 429)
        # A brand-new (never-answered) question is still blocked once the free
        # quota is spent — only reviewing answered questions stays free.
        fresh = Question.objects.create(
            question='Fresh?', choice_a='a', choice_b='b', choice_c='c', choice_d='d',
            answer='B', difficulty=3, question_type='Transitions',
        )
        answer_resp = self._answer(fresh)
        self.assertEqual(answer_resp.status_code, 429)
        self.assertEqual(answer_resp.data['error'], 'daily_limit')

    def test_practice_answer_records_attempt_and_updates_elo(self):
        before = self._stats('english').elo
        resp = self._answer(self.questions[0], 'b')  # correct
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['rated'])
        self.assertEqual(resp.data['sp_elo_rating_previous'], before)
        self.assertGreater(resp.data['sp_elo_rating'], before)
        self.assertGreater(resp.data['sp_elo_rating_delta'], 0)
        self.assertEqual(resp.data['quota']['used'], 1)
        self.assertEqual(resp.data['practice_stats']['answered'], 1)
        self.assertEqual(resp.data['practice_stats']['english_answered'], 1)
        self.assertEqual(resp.data['practice_stats']['english_correct'], 1)
        self.assertEqual(resp.data['practice_stats']['english_accuracy'], 100)
        self.assertEqual(resp.data['practice_stats']['math_answered'], 0)
        stats = self._stats('english')
        self.assertGreater(stats.elo, before)
        self.assertEqual((stats.answered, stats.correct), (1, 1))

    def test_math_practice_starts_fresh_from_its_own_elo_and_counts(self):
        from api.views.practice_views import MATH_QUESTION_TYPES
        for q in self.questions:  # 5 English answers through the real path
            self._answer(q, 'b')
        math_question = Question.objects.create(
            question='Solve x + 1 = 3.',
            choice_a='1',
            choice_b='2',
            choice_c='3',
            choice_d='4',
            answer='B',
            difficulty=3,
            question_type=MATH_QUESTION_TYPES[0],
        )
        english_before = self._stats('english').elo
        math_before = self._stats('math').elo
        self.assertEqual(math_before, 1200)

        resp = self._answer(math_question, '2')  # correct: choice_b's text

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['subject'], 'math')
        self.assertEqual(resp.data['sp_elo_rating_previous'], math_before)
        self.assertEqual(resp.data['sp_elo_rating_delta'], 16)
        self.assertEqual(resp.data['practice_stats']['english_answered'], 5)
        self.assertEqual(resp.data['practice_stats']['math_answered'], 1)
        self.assertEqual(self._stats('english').elo, english_before)
        self.assertGreater(self._stats('math').elo, math_before)

    def test_other_user_profile_returns_subject_attempt_counts(self):
        from api.models import PracticeStats
        other = User.objects.create_user(username='other-practicer', email='other@e.com')
        Profile.objects.create(user=other)
        PracticeStats.objects.create(user=other, subject='english', answered=1, correct=1)
        PracticeStats.objects.create(user=other, subject='math', answered=1, correct=0)

        resp = self.client.get(f'/api/profile/view_profile/{other.id}/')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['statistics']['english_answered'], 1)
        self.assertEqual(resp.data['statistics']['math_answered'], 1)
        self.assertEqual(resp.data['statistics']['practice_answered'], 2)
        self.assertEqual(resp.data['statistics']['english_accuracy'], 100)
        self.assertEqual(resp.data['statistics']['math_accuracy'], 0)

    def test_repeat_attempt_is_review_only(self):
        self._answer(self.questions[0], 'b')
        rating_after_first = self._stats('english').elo
        resp = self._answer(self.questions[0], 'b')
        # Re-answering an answered question is a review: no rating, no quota,
        # and no lifetime-counter impact.
        self.assertFalse(resp.data['rated'])
        self.assertTrue(resp.data['review'])
        self.assertEqual(resp.data['sp_elo_rating'], rating_after_first)
        self.assertEqual(self._stats('english').elo, rating_after_first)
        self.assertEqual(self._stats('english').answered, 1)

    def test_non_practice_mode_does_not_move_elo_or_quota(self):
        before = self._stats('english').elo
        resp = self._answer(self.questions[0], 'b', mode='')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('rated', resp.data)
        stats = self._stats('english')
        self.assertEqual(stats.elo, before)
        self.assertEqual(stats.answered, 0)
        from api.models import PracticeAttempt
        self.assertEqual(PracticeAttempt.objects.count(), 0)

    def test_wrong_answer_lowers_rating(self):
        before = self._stats('english').elo
        self._answer(self.questions[0], 'a')  # incorrect
        stats = self._stats('english')
        self.assertLess(stats.elo, before)
        self.assertEqual((stats.answered, stats.correct), (1, 0))

    def test_next_prefers_unattempted_questions(self):
        from api.models import PracticeAttempt
        for q in self.questions[:4]:
            PracticeAttempt.objects.create(user=self.user, question=q, correct=True)
        resp = self.client.get('/api/practice/next/')
        self.assertEqual(resp.data['question']['id'], self.questions[4].id)

    def test_next_does_not_repeat_after_finishing_a_topic(self):
        from api.models import PracticeAttempt
        for question in self.questions:
            PracticeAttempt.objects.create(user=self.user, question=question, correct=True)

        resp = self.client.get('/api/practice/next/')

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data['error'], 'completed_topic')

    def test_next_reports_when_a_premium_topic_has_no_questions(self):
        self.profile.is_premium = True
        self.profile.save(update_fields=['is_premium'])

        resp = self.client.get('/api/practice/next/', {'type': 'Inferences'})

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data['error'], 'no_questions')

    def test_practice_history_includes_selected_answer_and_review(self):
        self.questions[0].explanation = 'Because B is correct.'
        self.questions[0].save(update_fields=['explanation'])
        self._answer(self.questions[0], 'b')

        resp = self.client.get('/api/practice/history/')

        self.assertEqual(resp.status_code, 200)
        attempt = resp.data['attempts'][0]
        self.assertTrue(attempt['correct'])
        self.assertEqual(attempt['selected_choice'], 'b')
        self.assertEqual(attempt['question']['correct_choice_label'], 'B')
        self.assertEqual(attempt['question']['correct_answer'], 'b')
        self.assertEqual(attempt['question']['explanation'], 'Because B is correct.')

    def test_practice_history_filters_mistakes_by_question_type(self):
        from api.models import Question
        inference = Question.objects.create(
            question='Inference?', choice_a='a', choice_b='b',
            choice_c='c', choice_d='d', answer='B', difficulty=3,
            question_type='Inferences',
        )
        self._answer(self.questions[0], 'b')
        self._answer(inference, 'a')

        resp = self.client.get('/api/practice/history/', {
            'question_type': 'Inferences',
            'incorrect_only': 'true',
        })

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['attempts']), 1)
        self.assertEqual(resp.data['attempts'][0]['question']['id'], inference.id)
        self.assertEqual(resp.data['mistake_count'], 1)
        self.assertEqual(
            resp.data['question_types'], ['Inferences', 'Transitions'],
        )


class PracticeTypeProgressTests(APITestCase):
    """Question-bank progress: per-type solved/total counters for the
    practice page progress bars."""

    def setUp(self):
        from api.models import Question
        self.user = User.objects.create_user(username='banker', email='bank@e.com')
        self.profile = Profile.objects.create(user=self.user)
        self.client.force_authenticate(user=self.user)
        self.transitions = [
            Question.objects.create(
                question=f'T{i}?', choice_a='a', choice_b='b', choice_c='c', choice_d='d',
                answer='B', difficulty=3, question_type='Transitions',
            )
            for i in range(3)
        ]
        self.inference = Question.objects.create(
            question='I0?', choice_a='a', choice_b='b', choice_c='c', choice_d='d',
            answer='B', difficulty=3, question_type='Inferences',
        )

    def _answer(self, q, choice='b'):
        return self.client.post('/api/check_answer/', {
            'question_id': q.id, 'selected_choice': choice, 'mode': 'practice',
        }, format='json')

    def _entry(self, progress, question_type, subject='english'):
        return next(e for e in progress[subject] if e['type'] == question_type)

    def test_status_reports_totals_with_no_progress(self):
        resp = self.client.get('/api/practice/status/')
        self.assertEqual(resp.status_code, 200)
        progress = resp.data['type_progress']
        self.assertIn('english', progress)
        self.assertIn('math', progress)
        transitions = self._entry(progress, 'Transitions')
        self.assertEqual(transitions['total'], 3)
        self.assertEqual(transitions['solved'], 0)
        self.assertEqual(transitions['correct'], 0)
        # Every English type appears, even ones without questions yet.
        inferences = self._entry(progress, 'Inferences')
        self.assertEqual(inferences['total'], 1)

    def test_answers_move_the_right_type_counter(self):
        resp = self._answer(self.transitions[0], 'b')   # correct
        self.assertEqual(resp.status_code, 200)
        progress = resp.data['type_progress']
        self.assertEqual(self._entry(progress, 'Transitions')['solved'], 1)
        self.assertEqual(self._entry(progress, 'Transitions')['correct'], 1)

        resp = self._answer(self.transitions[1], 'a')   # wrong: solved, not correct
        progress = resp.data['type_progress']
        self.assertEqual(self._entry(progress, 'Transitions')['solved'], 2)
        self.assertEqual(self._entry(progress, 'Transitions')['correct'], 1)
        self.assertEqual(self._entry(progress, 'Inferences')['solved'], 0)

    def test_repeat_attempt_does_not_double_count(self):
        self._answer(self.transitions[0], 'b')
        resp = self._answer(self.transitions[0], 'b')
        progress = resp.data['type_progress']
        self.assertEqual(self._entry(progress, 'Transitions')['solved'], 1)
        self.assertEqual(self._entry(progress, 'Transitions')['correct'], 1)

    def test_solved_is_clamped_when_questions_are_deleted(self):
        from api.models import PracticeTypeStats
        from api.views.practice_views import practice_type_progress
        PracticeTypeStats.objects.create(
            user=self.user, question_type='Transitions', solved=5, correct=4,
        )
        entry = self._entry(practice_type_progress(self.user), 'Transitions')
        self.assertEqual(entry['total'], 3)
        self.assertEqual(entry['solved'], 3)
        self.assertEqual(entry['correct'], 3)


class PracticeFilterTests(APITestCase):
    """The premium practice filter bar: topic/level/saved narrowing plus the
    done-before/result review pool."""

    def setUp(self):
        from api.models import Question
        self.user = User.objects.create_user(username='filterer', email='f@e.com')
        self.profile = Profile.objects.create(user=self.user, is_premium=True)
        self.client.force_authenticate(user=self.user)
        # A spread of difficulties within one type so level filters can bite.
        self.by_level = {
            level: Question.objects.create(
                question=f'L{level}?', choice_a='a', choice_b='b', choice_c='c', choice_d='d',
                answer='B', difficulty=level, question_type='Transitions',
            )
            for level in range(1, 6)
        }

    def _answer(self, q, choice='b'):
        return self.client.post('/api/check_answer/', {
            'question_id': q.id, 'selected_choice': choice, 'mode': 'practice',
        }, format='json')

    def test_level_filter_only_serves_matching_difficulty(self):
        resp = self.client.get('/api/practice/next/', {'levels': '1,2'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(resp.data['question']['difficulty'], (1, 2))

    def test_filters_require_premium(self):
        self.profile.is_premium = False
        self.profile.save(update_fields=['is_premium'])
        resp = self.client.get('/api/practice/next/', {'levels': '3'})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data['error'], 'premium_required')

    def test_saved_only_filter_limits_to_saved(self):
        from api.models import SavedQuestion
        SavedQuestion.objects.create(user=self.user, question=self.by_level[4], subject='english')
        resp = self.client.get('/api/practice/next/', {'saved': 'only'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['question']['id'], self.by_level[4].id)

    def test_result_incorrect_reserves_the_review_pool(self):
        # Get one question wrong so it lands in the "incorrect" pool.
        self._answer(self.by_level[3], 'a')  # answer_text is 'b'
        resp = self.client.get('/api/practice/next/', {'result': 'incorrect'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['question']['id'], self.by_level[3].id)

    def test_reviewing_is_free_when_quota_is_spent(self):
        from api.models import PracticeAttempt, Question
        # Answer one wrong (records the incorrect attempt for the review pool).
        self._answer(self.by_level[3], 'a')
        # Exhaust the quota with a throwaway question so level 3's latest
        # recorded outcome stays "incorrect".
        filler = Question.objects.create(
            question='Filler?', choice_a='a', choice_b='b', choice_c='c', choice_d='d',
            answer='B', difficulty=3, question_type='Transitions',
        )
        for _ in range(25):
            PracticeAttempt.objects.create(user=self.user, question=filler, correct=True)
        resp = self.client.get('/api/practice/next/', {'result': 'incorrect'})
        self.assertEqual(resp.status_code, 200)
        review = self._answer(self.by_level[3], 'a')
        self.assertEqual(review.status_code, 200)
        self.assertTrue(review.data['review'])

    def test_review_leaves_the_result_pool_unchanged(self):
        # Wrong first, then answer it right in review. Because review records
        # nothing, the recorded outcome stays "incorrect" and the question is
        # still surfaced by the incorrect-only filter.
        self._answer(self.by_level[3], 'a')  # wrong (recorded)
        self._answer(self.by_level[3], 'b')  # right, but a review — not recorded
        resp = self.client.get('/api/practice/next/', {'result': 'incorrect'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['question']['id'], self.by_level[3].id)

    def test_empty_review_pool_reports_no_matches(self):
        resp = self.client.get('/api/practice/next/', {'result': 'incorrect'})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data['error'], 'no_matches')


class BillingViewsTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='subscriber', email='sub@example.com')
        self.profile = Profile.objects.create(user=self.user)
        self.client.force_authenticate(user=self.user)

    @override_settings(
        STRIPE_SECRET_KEY='stripe_secret_mock',
        STRIPE_PREMIUM_PRICE_ID='price_mock',
        STRIPE_API_VERSION='2026-06-24.dahlia',
        FRONTEND_URL='https://satduel.com',
        STRIPE_AUTOMATIC_TAX=False,
    )
    @patch('api.views.billing_views.stripe.checkout.Session.create')
    @patch('api.views.billing_views.stripe.Customer.create')
    def test_checkout_session_uses_subscription_checkout(self, mock_customer_create, mock_session_create):
        mock_customer_create.return_value = SimpleNamespace(id='cus_mock')
        mock_session_create.return_value = SimpleNamespace(url='https://checkout.stripe.test/session')

        resp = self.client.post(reverse('billing_create_checkout_session'), {}, format='json')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['url'], 'https://checkout.stripe.test/session')
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.stripe_customer_id, 'cus_mock')

        _, kwargs = mock_session_create.call_args
        self.assertEqual(kwargs['mode'], 'subscription')
        self.assertEqual(kwargs['customer'], 'cus_mock')
        self.assertEqual(kwargs['line_items'][0]['price'], 'price_mock')
        self.assertNotIn('payment_method_types', kwargs)

    @override_settings(
        STRIPE_SECRET_KEY='stripe_secret_mock',
        STRIPE_PREMIUM_PRICE_ID='price_mock',
        STRIPE_API_VERSION='2026-06-24.dahlia',
        FRONTEND_URL='https://satduel.com',
        STRIPE_AUTOMATIC_TAX=False,
    )
    @patch('api.views.billing_views.stripe.checkout.Session.create')
    @patch('api.views.billing_views.stripe.Customer.create')
    @patch('api.views.billing_views.stripe.Customer.retrieve')
    def test_checkout_recovers_from_stale_stripe_customer(self, mock_customer_retrieve, mock_customer_create, mock_session_create):
        missing_customer = Exception('No such customer: cus_test_old')
        missing_customer.code = 'resource_missing'
        mock_customer_retrieve.side_effect = missing_customer
        mock_customer_create.return_value = SimpleNamespace(id='cus_live_new')
        mock_session_create.return_value = SimpleNamespace(url='https://checkout.stripe.live/session')
        self.profile.stripe_customer_id = 'cus_test_old'
        self.profile.stripe_subscription_id = 'sub_test_old'
        self.profile.stripe_price_id = 'price_test_old'
        self.profile.is_premium = True
        self.profile.save()

        resp = self.client.post(reverse('billing_create_checkout_session'), {}, format='json')

        self.assertEqual(resp.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.stripe_customer_id, 'cus_live_new')
        self.assertIsNone(self.profile.stripe_subscription_id)
        self.assertIsNone(self.profile.stripe_price_id)
        self.assertFalse(self.profile.is_premium)
        _, kwargs = mock_session_create.call_args
        self.assertEqual(kwargs['customer'], 'cus_live_new')

    @override_settings(
        STRIPE_SECRET_KEY='stripe_secret_mock',
        STRIPE_PORTAL_CONFIGURATION_ID='bpc_test_old',
        STRIPE_API_VERSION='2026-06-24.dahlia',
        FRONTEND_URL='https://satduel.com',
    )
    @patch('api.views.billing_views.StripeError', Exception)
    @patch('api.views.billing_views.stripe.billing_portal.Session.create')
    @patch('api.views.billing_views.stripe.Customer.retrieve')
    def test_portal_retries_without_stale_configuration(self, mock_customer_retrieve, mock_session_create):
        self.profile.stripe_customer_id = 'cus_live_mock'
        self.profile.save(update_fields=['stripe_customer_id'])
        mock_customer_retrieve.return_value = SimpleNamespace(id='cus_live_mock', deleted=False)
        missing_configuration = Exception("No such configuration: 'bpc_test_old'")
        missing_configuration.code = 'resource_missing'
        mock_session_create.side_effect = [
            missing_configuration,
            SimpleNamespace(url='https://billing.stripe.test/session'),
        ]

        resp = self.client.post(reverse('billing_create_portal_session'), {}, format='json')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['url'], 'https://billing.stripe.test/session')
        self.assertEqual(mock_session_create.call_count, 2)
        first_call = mock_session_create.call_args_list[0].kwargs
        second_call = mock_session_create.call_args_list[1].kwargs
        self.assertEqual(first_call['configuration'], 'bpc_test_old')
        self.assertNotIn('configuration', second_call)

    @override_settings(STRIPE_SECRET_KEY='', STRIPE_PREMIUM_PRICE_ID='')
    def test_checkout_requires_stripe_config(self):
        resp = self.client.post(reverse('billing_create_checkout_session'), {}, format='json')

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.data['error'], 'stripe_not_configured')
        self.assertIn('STRIPE_SECRET_KEY', resp.data['missing'])
        self.assertIn('STRIPE_PREMIUM_PRICE_ID', resp.data['missing'])

    @override_settings(
        STRIPE_SECRET_KEY='stripe_secret_mock',
        STRIPE_WEBHOOK_SECRET='webhook_secret_mock',
        STRIPE_API_VERSION='2026-06-24.dahlia',
    )
    @patch('api.views.billing_views.stripe.Subscription.retrieve')
    @patch('api.views.billing_views.stripe.Webhook.construct_event')
    def test_checkout_webhook_grants_premium(self, mock_construct_event, mock_subscription_retrieve):
        period_end = int((timezone.now() + timedelta(days=30)).timestamp())
        mock_construct_event.return_value = {
            'type': 'checkout.session.completed',
            'data': {'object': {
                'mode': 'subscription',
                'client_reference_id': str(self.user.id),
                'customer': 'cus_mock',
                'subscription': 'sub_mock',
            }},
        }
        mock_subscription_retrieve.return_value = {
            'id': 'sub_mock',
            'customer': 'cus_mock',
            'status': 'active',
            'current_period_end': period_end,
            'items': {'data': [{'price': {'id': 'price_mock'}}]},
        }

        resp = self.client.post(
            reverse('stripe_webhook'),
            data='{}',
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='sig_mock',
        )

        self.assertEqual(resp.status_code, 200)
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.is_premium)
        self.assertEqual(self.profile.stripe_customer_id, 'cus_mock')
        self.assertEqual(self.profile.stripe_subscription_id, 'sub_mock')
        self.assertEqual(self.profile.stripe_price_id, 'price_mock')

    @override_settings(
        STRIPE_SECRET_KEY='stripe_secret_mock',
        STRIPE_WEBHOOK_SECRET='webhook_secret_mock',
        STRIPE_API_VERSION='2026-06-24.dahlia',
    )
    @patch('api.views.billing_views.stripe.Webhook.construct_event')
    def test_subscription_deleted_webhook_revokes_premium(self, mock_construct_event):
        self.profile.is_premium = True
        self.profile.stripe_customer_id = 'cus_mock'
        self.profile.stripe_subscription_id = 'sub_mock'
        self.profile.save()
        mock_construct_event.return_value = {
            'type': 'customer.subscription.deleted',
            'data': {'object': {
                'id': 'sub_mock',
                'customer': 'cus_mock',
                'status': 'canceled',
            }},
        }

        resp = self.client.post(
            reverse('stripe_webhook'),
            data='{}',
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='sig_mock',
        )

        self.assertEqual(resp.status_code, 200)
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.is_premium)


class TournamentHistoryTests(APITestCase):
    def setUp(self):
        from api.models import Tournament, TournamentParticipation, Question
        from django.utils import timezone
        self.user = User.objects.create_user(username='hist', email='h@e.com')
        Profile.objects.create(user=self.user)
        self.client.force_authenticate(user=self.user)
        q = Question.objects.create(
            question='Q?', choice_a='a', choice_b='b', choice_c='c', choice_d='d',
            answer='A', difficulty=2, question_type='Transitions',
        )
        for i in range(15):
            t = Tournament.objects.create(
                name=f'T{i}', description='d', start_time=timezone.now(),
            )
            t.questions.add(q)
            TournamentParticipation.objects.create(
                user=self.user, tournament=t, status='Completed',
                start_time=timezone.now(), score=i,
            )

    def test_history_is_paginated(self):
        resp = self.client.get('/api/tournaments/get_history/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 10)  # default page size
        self.assertEqual(resp['X-Total-Count'], '15')
        page2 = self.client.get('/api/tournaments/get_history/', {'page': 2})
        self.assertEqual(len(page2.data), 5)

    def test_history_shape_is_backward_compatible(self):
        resp = self.client.get('/api/tournaments/get_history/')
        entry = resp.data[0]
        self.assertIn('tournament', entry)
        self.assertIn('participantNumber', entry['tournament'])
        self.assertIn('questionNumber', entry['tournament'])
        self.assertEqual(entry['tournament']['participantNumber'], 1)

    def test_history_query_count_is_constant(self):
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        with CaptureQueriesContext(connection) as ctx:
            self.client.get('/api/tournaments/get_history/')
        # Must not grow with the number of participations (was 2N+1).
        self.assertLess(len(ctx), 10)


class TournamentCreateTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='host', email='host@example.com')
        Profile.objects.create(user=self.user)
        self.client.force_authenticate(user=self.user)

    def test_user_created_private_tournament_is_saved_to_profile(self):
        now = timezone.now()
        resp = self.client.post('/api/tournaments/create/', {
            'name': 'Study group round',
            'description': 'Private practice',
            'start_time': now.isoformat(),
            'end_time': (now + timedelta(days=1)).isoformat(),
            'duration': '00:15:00',
            'private': True,
            'questions': [{
                'question': 'Q?',
                'choice_a': 'a',
                'choice_b': 'b',
                'choice_c': 'c',
                'choice_d': 'd',
                'answer': 'A',
                'difficulty': 1,
                'question_type': 'Words in Context',
                'explanation': 'Because.',
            }],
        }, format='json')

        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data['private'])
        self.assertTrue(resp.data['join_code'])
        self.assertEqual(resp.data['duration'], '00:15:00')
        self.assertEqual(self.user.profile.my_tournaments.count(), 1)


class QuestionAnswerLeakTests(APITestCase):
    def setUp(self):
        from api.models import Question
        self.q = Question.objects.create(
            question='Q?', choice_a='a', choice_b='b', choice_c='c', choice_d='d',
            answer='B', difficulty=3, question_type='Transitions', explanation='because',
        )

    def test_public_list_has_no_answer(self):
        resp = self.client.get('/api/questions/?num=1')
        self.assertEqual(resp.status_code, 200)
        if resp.data:
            self.assertNotIn('answer', resp.data[0])
            self.assertNotIn('explanation', resp.data[0])


class GetAnswerLockdownTests(APITestCase):
    def setUp(self):
        from api.models import Question
        self.q = Question.objects.create(
            question='Q?', choice_a='a', choice_b='b', choice_c='c', choice_d='d',
            answer='B', difficulty=3, question_type='Transitions', explanation='why',
        )
        self.user = User.objects.create_user(username='dave', email='d@e.com')
        Profile.objects.create(user=self.user)

    def test_anonymous_denied(self):
        resp = self.client.post('/api/get_answer/', {'question_id': self.q.id}, format='json')
        self.assertEqual(resp.status_code, 401)

    def test_authenticated_allowed(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post('/api/get_answer/', {'question_id': self.q.id}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['answer_choice'], 'B')

    def test_blocked_during_active_tournament(self):
        from api.models import Tournament, TournamentParticipation
        from django.utils import timezone
        t = Tournament.objects.create(
            name='T', description='d', start_time=timezone.now(),
        )
        t.questions.add(self.q)
        TournamentParticipation.objects.create(user=self.user, tournament=t, status='Active')
        self.client.force_authenticate(user=self.user)
        resp = self.client.post('/api/get_answer/', {'question_id': self.q.id}, format='json')
        self.assertEqual(resp.status_code, 403)

    def test_allowed_after_tournament_finished(self):
        from api.models import Tournament, TournamentParticipation
        from django.utils import timezone
        t = Tournament.objects.create(
            name='T', description='d', start_time=timezone.now(),
        )
        t.questions.add(self.q)
        TournamentParticipation.objects.create(user=self.user, tournament=t, status='Completed')
        self.client.force_authenticate(user=self.user)
        resp = self.client.post('/api/get_answer/', {'question_id': self.q.id}, format='json')
        self.assertEqual(resp.status_code, 200)


class PracticeStreakTests(APITestCase):
    """Day streak: 10 practice answers in a local day extend the flame."""

    def setUp(self):
        from api.models import Question
        self.user = User.objects.create_user(username='streaker', email='s@e.com')
        self.profile = Profile.objects.create(user=self.user)
        self.client.force_authenticate(user=self.user)
        self.questions = [
            Question.objects.create(
                question=f'SQ{i}?', choice_a='a', choice_b='b', choice_c='c', choice_d='d',
                answer='A', difficulty=3, question_type='Transitions',
            )
            for i in range(12)
        ]

    def _answer(self, q):
        return self.client.post('/api/check_answer/', {
            'question_id': q.id, 'selected_choice': 'a', 'mode': 'practice',
        }, format='json')

    def _backdate_attempts(self, days_ago, n=10):
        """Create n attempts on a past day (UTC profile default)."""
        from api.models import PracticeAttempt
        from django.utils import timezone as djtz
        from datetime import timedelta
        when = djtz.now() - timedelta(days=days_ago)
        for i in range(n):
            a = PracticeAttempt.objects.create(
                user=self.user, question=self.questions[i], correct=True,
            )
            PracticeAttempt.objects.filter(pk=a.pk).update(created_at=when)

    def test_streak_extends_on_goal_completion(self):
        for i in range(9):
            resp = self._answer(self.questions[i])
            self.assertFalse(resp.data['daily']['completed_today'])
            self.assertFalse(resp.data['daily']['streak_extended'])
        resp = self._answer(self.questions[9])  # 10th answer
        self.assertTrue(resp.data['daily']['completed_today'])
        self.assertTrue(resp.data['daily']['streak_extended'])
        self.assertEqual(resp.data['daily']['streak'], 1)
        self.assertIn('day_ends_at', resp.data['daily'])

    def test_local_day_bounds_follow_daylight_saving_time(self):
        import datetime
        from api.views.practice_views import _local_day_bounds_utc

        self.profile.timezone = 'America/New_York'
        start, end = _local_day_bounds_utc(self.profile, datetime.date(2026, 3, 8))

        self.assertEqual(end - start, datetime.timedelta(hours=23))

    def test_profile_activity_tracks_answers_not_logins(self):
        self._answer(self.questions[0])

        resp = self.client.get('/api/infinite_questions_profile/')

        self.assertEqual(len(resp.data['activity']), 365)
        self.assertEqual(resp.data['activity'][-1]['count'], 1)

    def test_profile_keeps_best_correct_streak_after_a_miss(self):
        for question in self.questions[:3]:
            self._answer(question)
        self.client.post('/api/check_answer/', {
            'question_id': self.questions[3].id,
            'selected_choice': 'b',
            'mode': 'practice',
        }, format='json')

        resp = self.client.get('/api/profile/')
        stats = self.client.get('/api/infinite_questions_profile/')

        self.assertEqual(resp.data['max_streak'], 3)
        self.assertEqual(stats.data['current_streak'], 0)

    def test_no_double_extension_same_day(self):
        for i in range(10):
            self._answer(self.questions[i])
        resp = self._answer(self.questions[10])  # 11th
        self.assertFalse(resp.data['daily']['streak_extended'])
        self.assertEqual(resp.data['daily']['streak'], 1)

    def test_consecutive_days_increment(self):
        from api.views.practice_views import update_daily_streak
        import datetime
        # Simulate: completed yesterday with streak 3
        self.profile.practice_streak = 3
        self.profile.longest_practice_streak = 3
        self.profile.last_practice_completed = (
            datetime.date.today() - datetime.timedelta(days=1)
        )
        self.profile.save()
        for i in range(10):
            self._answer(self.questions[i])
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.practice_streak, 4)
        self.assertEqual(self.profile.longest_practice_streak, 4)

    def test_gap_resets_to_one(self):
        import datetime
        self.profile.practice_streak = 7
        self.profile.longest_practice_streak = 7
        self.profile.last_practice_completed = (
            datetime.date.today() - datetime.timedelta(days=3)
        )
        self.profile.save()
        for i in range(10):
            self._answer(self.questions[i])
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.practice_streak, 1)
        self.assertEqual(self.profile.longest_practice_streak, 7)  # record kept

    def test_effective_streak_zero_after_missed_day(self):
        import datetime
        self.profile.practice_streak = 5
        self.profile.last_practice_completed = (
            datetime.date.today() - datetime.timedelta(days=2)
        )
        self.profile.save()
        resp = self.client.get('/api/practice/status/')
        self.assertEqual(resp.data['daily']['streak'], 0)
        self.assertEqual(resp.data['daily']['longest'], 0)

    def test_status_includes_week_strip(self):
        self._backdate_attempts(days_ago=1, n=10)
        resp = self.client.get('/api/practice/status/')
        week = resp.data['daily']['week']
        self.assertEqual(len(week), 7)
        self.assertTrue(week[5]['completed'])   # yesterday hit the goal
        self.assertTrue(week[6]['is_today'])
        self.assertFalse(week[6]['completed'])

    def test_user_streak_endpoint_returns_practice_streak(self):
        resp = self.client.get('/api/user_streak/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('streak', resp.data)
        self.assertIn('goal', resp.data)
        self.assertIn('week', resp.data)


class PartyModeTests(APITestCase):
    def setUp(self):
        self.host = User.objects.create_user(username='host', password='x')
        Profile.objects.create(user=self.host)
        self.guest = User.objects.create_user(username='guest', password='x')
        Profile.objects.create(user=self.guest)
        for i in range(12):
            Question.objects.create(
                question=f'Q{i}?', choice_a='a', choice_b='b', choice_c='c', choice_d='d',
                answer='A', difficulty=(i % 3) + 2, question_type='Transitions',
            )

    def _create(self, **overrides):
        self.client.force_authenticate(user=self.host)
        payload = {'num_questions': 5, 'seconds_per_question': 60, 'subject': 'english',
                   'difficulty': 'medium', **overrides}
        resp = self.client.post(reverse('party_create'), payload, format='json')
        self.assertEqual(resp.status_code, 201)
        return resp.data

    def test_free_tier_caps_are_enforced(self):
        data = self._create(max_players=30, num_questions=40)
        from api.models import PartyRoom
        room = PartyRoom.objects.get(id=data['id'])
        self.assertEqual(room.max_players, 6)
        self.assertEqual(room.num_questions, 20)

    def test_premium_caps(self):
        self.host.profile.is_premium = True
        self.host.profile.save()
        data = self._create(max_players=30, num_questions=40)
        from api.models import PartyRoom
        room = PartyRoom.objects.get(id=data['id'])
        self.assertEqual(room.max_players, 30)
        self.assertEqual(room.num_questions, 40)

    def test_party_reactions_use_the_duel_reaction_table_and_premium_loadout(self):
        data = self._create()
        sent = self.client.post(
            reverse('party_emote', args=[data['id']]),
            {'reactions': [{'emoji': '👍', 'count': 3}]},
            format='json',
        )
        self.assertEqual(sent.status_code, 200)
        self.assertEqual(len(sent.data['reactions']), 3)

        reaction = DuelEmote.objects.get(id=sent.data['reaction']['id'])
        self.assertIsNone(reaction.room_id)
        self.assertEqual(reaction.party_room_id, data['id'])
        state = self.client.get(reverse('party_state', args=[data['id']])).data
        self.assertEqual(state['reactions'][0]['sender_username'], 'host')
        self.assertEqual(len(state['reactions']), 3)
        self.assertEqual(len(state['your_emotes']), 4)

        self.host.profile.duel_emotes = ['🤡', '👍', '🔥', '😂']
        self.host.profile.save(update_fields=['duel_emotes'])
        denied = self.client.post(reverse('party_emote', args=[data['id']]), {'emoji': '🤡'}, format='json')
        self.assertEqual(denied.status_code, 400)

        self.host.profile.is_premium = True
        self.host.profile.save(update_fields=['is_premium'])
        DuelEmote.objects.filter(party_room_id=data['id']).update(created_at=timezone.now() - timedelta(seconds=2))
        allowed = self.client.post(reverse('party_emote', args=[data['id']]), {'emoji': '🤡'}, format='json')
        self.assertEqual(allowed.status_code, 200)

        too_many = self.client.post(
            reverse('party_emote', args=[data['id']]),
            {'reactions': [{'emoji': '👍', 'count': 12}, {'emoji': '🔥', 'count': 12}]},
            format='json',
        )
        self.assertEqual(too_many.status_code, 400)

        self.client.force_authenticate(user=self.guest)
        outsider = self.client.post(reverse('party_emote', args=[data['id']]), {'emoji': '👍'}, format='json')
        self.assertEqual(outsider.status_code, 403)

    def test_full_game_flow(self):
        from api.models import PartyRoom
        data = self._create()
        room_id = data['id']

        # Guest joins with the code.
        self.client.force_authenticate(user=self.guest)
        resp = self.client.post(reverse('party_join'), {'code': data['code']}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['id'], room_id)

        # Wrong code 404s.
        bad = self.client.post(reverse('party_join'), {'code': '000000x'}, format='json')
        self.assertEqual(bad.status_code, 404)

        # Only the host can start.
        resp = self.client.post(reverse('party_start', args=[room_id]))
        self.assertEqual(resp.status_code, 404)
        self.client.force_authenticate(user=self.host)
        resp = self.client.post(reverse('party_start', args=[room_id]))
        self.assertEqual(resp.status_code, 200)

        # Countdown phase, then flip to question by rewinding the clock.
        state = self.client.get(reverse('party_state', args=[room_id])).data
        self.assertEqual(state['status'], 'countdown')
        room = PartyRoom.objects.get(id=room_id)
        room.phase_started_at -= timedelta(seconds=10)
        room.save()
        state = self.client.get(reverse('party_state', args=[room_id])).data
        self.assertEqual(state['status'], 'question')
        self.assertIn('question', state)
        self.assertEqual(len(state['question']['choices']), 4)
        self.assertNotIn('answer', state['question'])

        # Host answers correctly, gets 500-1000 points.
        resp = self.client.post(reverse('party_answer', args=[room_id]), {'choice': 'A'}, format='json')
        self.assertEqual(resp.status_code, 200)
        dup = self.client.post(reverse('party_answer', args=[room_id]), {'choice': 'A'}, format='json')
        self.assertEqual(dup.status_code, 400)

        # Guest answers wrong: 0 points. Everyone answered -> leaderboard.
        self.client.force_authenticate(user=self.guest)
        self.client.post(reverse('party_answer', args=[room_id]), {'choice': 'B'}, format='json')
        state = self.client.get(reverse('party_state', args=[room_id])).data
        self.assertEqual(state['status'], 'leaderboard')
        self.assertFalse(state['reveal']['correct'])
        self.assertEqual(state['reveal']['correct_choice'], 'A')
        scores = {p['username']: p['score'] for p in state['players']}
        self.assertEqual(scores['guest'], 0)
        self.assertGreaterEqual(scores['host'], 500)
        self.assertEqual(state['players'][0]['username'], 'host')

        # Only host advances; play through to the podium.
        resp = self.client.post(reverse('party_next', args=[room_id]))
        self.assertEqual(resp.status_code, 404)
        self.client.force_authenticate(user=self.host)
        for i in range(1, 5):
            resp = self.client.post(reverse('party_next', args=[room_id]))
            self.assertEqual(resp.data['status'], 'question')
            room = PartyRoom.objects.get(id=room_id)
            room.phase_started_at -= timedelta(seconds=120)  # let the timer expire
            room.save()
            state = self.client.get(reverse('party_state', args=[room_id])).data
            self.assertEqual(state['status'], 'leaderboard')
        self.assertTrue(state['reveal']['is_last'])
        resp = self.client.post(reverse('party_next', args=[room_id]))
        self.assertEqual(resp.data['status'], 'finished')

    def test_cannot_join_started_or_full_room(self):
        data = self._create(max_players=2)
        outsider = User.objects.create_user(username='late', password='x')
        Profile.objects.create(user=outsider)

        self.client.force_authenticate(user=self.guest)
        self.client.post(reverse('party_join'), {'code': data['code']}, format='json')
        self.client.force_authenticate(user=outsider)
        resp = self.client.post(reverse('party_join'), {'code': data['code']}, format='json')
        self.assertEqual(resp.status_code, 400)  # full

        self.client.force_authenticate(user=self.host)
        self.client.post(reverse('party_start', args=[data['id']]))
        resp = self.client.get(reverse('party_state', args=[data['id']]))
        self.assertEqual(resp.status_code, 200)

        # Outsider can't poll a room they're not in.
        self.client.force_authenticate(user=outsider)
        resp = self.client.get(reverse('party_state', args=[data['id']]))
        self.assertEqual(resp.status_code, 403)

    def test_new_room_supersedes_hosts_old_room(self):
        first = self._create()
        second = self._create()
        from api.models import PartyRoom
        self.assertEqual(PartyRoom.objects.get(id=first['id']).status, 'finished')
        self.assertEqual(PartyRoom.objects.get(id=second['id']).status, 'lobby')
        # The old code no longer matches on join.
        self.client.force_authenticate(user=self.guest)
        resp = self.client.post(reverse('party_join'), {'code': first['code']}, format='json')
        self.assertEqual(resp.status_code, 404)

    def test_creating_a_room_does_not_delete_old_party_history(self):
        from api.models import PartyRoom
        first = self._create()
        PartyRoom.objects.filter(id=first['id']).update(
            status='finished', created_at=timezone.now() - timedelta(days=3),
        )
        self._create()
        self.assertTrue(PartyRoom.objects.filter(id=first['id']).exists())

    def test_history_lists_hosted_and_participated_rooms_with_review(self):
        from api.models import PartyPlayer, PartyRoom
        question = Question.objects.first()
        room = PartyRoom.objects.create(
            host=self.host,
            original_host=self.host,
            code='123456',
            status='finished',
            question_ids=[question.id],
            num_questions=1,
        )
        PartyPlayer.objects.create(
            room=room,
            user=self.host,
            score=900,
            answers={'0': {'choice': 'A', 'correct': True, 'points': 900}},
        )
        PartyPlayer.objects.create(
            room=room,
            user=self.guest,
            score=0,
            answers={'0': {'choice': 'B', 'correct': False, 'points': 0}},
        )

        self.client.force_authenticate(user=self.host)
        hosted = self.client.get(reverse('party_history')).data
        self.assertEqual(hosted[0]['role'], 'hosted')
        self.assertEqual(hosted[0]['winners'], ['host'])

        self.client.force_authenticate(user=self.guest)
        participated = self.client.get(reverse('party_history')).data
        self.assertEqual(participated[0]['role'], 'participated')
        detail = self.client.get(reverse('party_history_detail', args=[room.id])).data
        self.assertEqual(detail['your_score'], 0)
        self.assertEqual(detail['questions'][0]['your_choice'], 'B')
        self.assertFalse(detail['questions'][0]['correct'])
        self.assertEqual(detail['questions'][0]['correct_choice'], 'A')

    def test_party_history_is_private_to_players(self):
        from api.models import PartyPlayer, PartyRoom
        question = Question.objects.first()
        room = PartyRoom.objects.create(
            host=self.host,
            original_host=self.host,
            code='654321',
            status='finished',
            question_ids=[question.id],
        )
        PartyPlayer.objects.create(room=room, user=self.host)
        outsider = User.objects.create_user(username='outsider', password='x')
        Profile.objects.create(user=outsider)
        self.client.force_authenticate(user=outsider)
        response = self.client.get(reverse('party_history_detail', args=[room.id]))
        self.assertEqual(response.status_code, 403)

    def test_host_leaving_hands_off_or_closes_room(self):
        from api.models import PartyRoom
        data = self._create()
        self.client.force_authenticate(user=self.guest)
        self.client.post(reverse('party_join'), {'code': data['code']}, format='json')

        # Host leaves -> guest inherits the room.
        self.client.force_authenticate(user=self.host)
        self.client.post(reverse('party_leave', args=[data['id']]))
        room = PartyRoom.objects.get(id=data['id'])
        self.assertEqual(room.host, self.guest)
        self.assertEqual(room.status, 'lobby')

        # Last player leaves -> room closes.
        self.client.force_authenticate(user=self.guest)
        self.client.post(reverse('party_leave', args=[data['id']]))
        self.assertEqual(PartyRoom.objects.get(id=data['id']).status, 'finished')

    def test_leaving_a_started_room_keeps_the_player_for_history(self):
        from api.models import PartyRoom
        data = self._create()
        self.client.force_authenticate(user=self.guest)
        self.client.post(reverse('party_join'), {'code': data['code']}, format='json')
        PartyRoom.objects.filter(id=data['id']).update(status='question', question_ids=[Question.objects.first().id])
        self.client.post(reverse('party_leave', args=[data['id']]))
        self.assertTrue(PartyRoom.objects.get(id=data['id']).players.filter(user=self.guest).exists())

    def test_stale_host_is_replaced_on_poll(self):
        from api.models import PartyPlayer, PartyRoom
        data = self._create()
        self.client.force_authenticate(user=self.guest)
        self.client.post(reverse('party_join'), {'code': data['code']}, format='json')

        # Host swiped away 2 minutes ago; guest keeps polling.
        PartyPlayer.objects.filter(room_id=data['id'], user=self.host).update(
            last_seen=timezone.now() - timedelta(seconds=120))
        state = self.client.get(reverse('party_state', args=[data['id']])).data
        self.assertTrue(state['is_host'])
        room = PartyRoom.objects.get(id=data['id'])
        self.assertEqual(room.host, self.guest)
        # The stale lobby seat was freed.
        self.assertEqual(room.players.count(), 1)

    def test_fully_abandoned_lobby_closes_on_join_attempt(self):
        from api.models import PartyPlayer, PartyRoom
        data = self._create()
        PartyPlayer.objects.filter(room_id=data['id']).update(
            last_seen=timezone.now() - timedelta(seconds=120))
        self.client.force_authenticate(user=self.guest)
        resp = self.client.post(reverse('party_join'), {'code': data['code']}, format='json')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(PartyRoom.objects.get(id=data['id']).status, 'finished')


class PartyTeamsModeTests(APITestCase):
    """Teams mode: seating, renaming, and team standings."""

    def setUp(self):
        self.users = []
        for i in range(5):
            user = User.objects.create_user(username=f'p{i}', password='x')
            Profile.objects.create(user=user)
            self.users.append(user)
        self.host = self.users[0]
        for i in range(12):
            Question.objects.create(
                question=f'Q{i}?', choice_a='a', choice_b='b', choice_c='c', choice_d='d',
                answer='A', difficulty=(i % 3) + 2, question_type='Transitions',
            )

    def _room(self, joiners, **overrides):
        self.client.force_authenticate(user=self.host)
        payload = {'mode': 'teams', 'num_questions': 3, 'seconds_per_question': 60,
                   'subject': 'english', 'difficulty': 'medium', **overrides}
        data = self.client.post(reverse('party_create'), payload, format='json').data
        for user in self.users[1:joiners + 1]:
            self.client.force_authenticate(user=user)
            self.client.post(reverse('party_join'), {'code': data['code']}, format='json')
        self.client.force_authenticate(user=self.host)
        return data['id']

    def _teams_of(self, room_id):
        from api.models import PartyRoom
        room = PartyRoom.objects.get(id=room_id)
        return [p.team for p in room.players.order_by('id')]

    def test_random_teams_are_balanced_at_kickoff(self):
        room_id = self._room(joiners=3, num_teams=2, random_teams=True)
        self.assertEqual(self._teams_of(room_id), [None] * 4)  # unseated until start
        self.assertEqual(self.client.post(reverse('party_start', args=[room_id])).status_code, 200)
        seats = self._teams_of(room_id)
        self.assertNotIn(None, seats)
        self.assertEqual(sorted(seats), [0, 0, 1, 1])

    def test_four_teams_split_five_players_evenly_as_possible(self):
        room_id = self._room(joiners=4, num_teams=4, random_teams=True)
        self.client.post(reverse('party_start', args=[room_id]))
        counts = sorted(self._teams_of(room_id).count(i) for i in range(4))
        self.assertEqual(counts, [1, 1, 1, 2])

    def test_host_seats_players_by_hand_and_stragglers_get_filled(self):
        room_id = self._room(joiners=3, num_teams=2, random_teams=False)
        resp = self.client.post(
            reverse('party_teams', args=[room_id]),
            {'assignments': {str(self.users[1].id): 1, str(self.users[2].id): 1}},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.client.post(reverse('party_start', args=[room_id]))
        from api.models import PartyRoom
        room = PartyRoom.objects.get(id=room_id)
        # Hand-picked seats survive; the two unsorted players land on the empty team.
        self.assertEqual(room.players.get(user=self.users[1]).team, 1)
        self.assertEqual(room.players.get(user=self.users[2]).team, 1)
        self.assertEqual(sorted(self._teams_of(room_id)), [0, 0, 1, 1])

    def test_switching_to_random_clears_hand_picked_seats(self):
        room_id = self._room(joiners=3, num_teams=2, random_teams=False)
        self.client.post(reverse('party_teams', args=[room_id]),
                         {'assignments': {str(self.users[1].id): 1}}, format='json')
        self.client.post(reverse('party_teams', args=[room_id]),
                         {'random_teams': True}, format='json')
        self.assertEqual(self._teams_of(room_id), [None] * 4)

    def test_shrinking_team_count_strands_players_on_dropped_teams(self):
        room_id = self._room(joiners=3, num_teams=4, random_teams=False)
        self.client.post(reverse('party_teams', args=[room_id]),
                         {'assignments': {str(self.users[1].id): 3, str(self.users[2].id): 1}},
                         format='json')
        self.client.post(reverse('party_teams', args=[room_id]), {'num_teams': 2}, format='json')
        from api.models import PartyRoom
        room = PartyRoom.objects.get(id=room_id)
        self.assertIsNone(room.players.get(user=self.users[1]).team)  # team 3 is gone
        self.assertEqual(room.players.get(user=self.users[2]).team, 1)  # team 1 survives

    def test_renamed_teams_show_up_in_state(self):
        room_id = self._room(joiners=1, num_teams=2, random_teams=True)
        self.client.post(reverse('party_teams', args=[room_id]),
                         {'names': ['Nerds', 'Jocks']}, format='json')
        state = self.client.get(reverse('party_state', args=[room_id])).data
        self.assertEqual([t['name'] for t in sorted(state['teams'], key=lambda t: t['index'])],
                         ['Nerds', 'Jocks'])

    def test_unnamed_teams_fall_back_to_defaults(self):
        room_id = self._room(joiners=1, num_teams=2, random_teams=True)
        state = self.client.get(reverse('party_state', args=[room_id])).data
        self.assertEqual([t['name'] for t in sorted(state['teams'], key=lambda t: t['index'])],
                         ['Team A', 'Team B'])

    def test_start_blocked_when_players_cannot_fill_the_teams(self):
        room_id = self._room(joiners=1, num_teams=4, random_teams=True)
        resp = self.client.post(reverse('party_start', args=[room_id]))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('at least 4 players', resp.data['error'])

    def test_only_the_host_can_move_players(self):
        room_id = self._room(joiners=1, num_teams=2, random_teams=False)
        self.client.force_authenticate(user=self.users[1])
        resp = self.client.post(reverse('party_teams', args=[room_id]),
                                {'names': ['Hacked', 'Hacked']}, format='json')
        self.assertEqual(resp.status_code, 404)

    def test_teams_lock_once_the_game_starts(self):
        room_id = self._room(joiners=1, num_teams=2, random_teams=False)
        self.client.post(reverse('party_start', args=[room_id]))
        resp = self.client.post(reverse('party_teams', args=[room_id]),
                                {'names': ['Late', 'Rename']}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_team_score_sums_members_and_keeps_the_breakdown(self):
        from api.models import PartyRoom
        room_id = self._room(joiners=1, num_teams=2, random_teams=False)
        self.client.post(reverse('party_teams', args=[room_id]),
                         {'assignments': {str(self.host.id): 0, str(self.users[1].id): 0}},
                         format='json')
        self.client.post(reverse('party_start', args=[room_id]))
        room = PartyRoom.objects.get(id=room_id)
        room.players.filter(user=self.host).update(score=700)
        room.players.filter(user=self.users[1]).update(score=300)

        state = self.client.get(reverse('party_state', args=[room_id])).data
        top = state['teams'][0]
        self.assertEqual(top['score'], 1000)
        self.assertEqual([m['username'] for m in top['members']], ['p0', 'p1'])  # ranked
        self.assertEqual(state['teams'][1]['score'], 0)

    def test_teams_endpoint_rejects_non_team_rooms(self):
        self.client.force_authenticate(user=self.host)
        data = self.client.post(reverse('party_create'),
                                {'mode': 'classic', 'subject': 'english'}, format='json').data
        resp = self.client.post(reverse('party_teams', args=[data['id']]),
                                {'names': ['a', 'b']}, format='json')
        self.assertEqual(resp.status_code, 400)


class PartyRevealTests(APITestCase):
    """The post-question reveal payload: answer spread and the review card."""

    def setUp(self):
        self.host = User.objects.create_user(username='host', password='x')
        Profile.objects.create(user=self.host)
        self.guest = User.objects.create_user(username='guest', password='x')
        Profile.objects.create(user=self.guest)
        Question.objects.create(
            question='Pick A?', choice_a='right', choice_b='wrong-b', choice_c='c', choice_d='d',
            answer='A', difficulty=3, question_type='Transitions', explanation='Because A.',
        )

    def test_reveal_carries_distribution_and_review(self):
        self.client.force_authenticate(user=self.host)
        data = self.client.post(reverse('party_create'),
                                {'num_questions': 1, 'subject': 'english',
                                 'seconds_per_question': 60}, format='json').data
        room_id = data['id']
        self.client.force_authenticate(user=self.guest)
        self.client.post(reverse('party_join'), {'code': data['code']}, format='json')
        self.client.force_authenticate(user=self.host)
        self.client.post(reverse('party_start', args=[room_id]))

        from api.models import PartyRoom
        room = PartyRoom.objects.get(id=room_id)
        room.status = 'question'
        room.phase_started_at = timezone.now()
        room.save()

        self.client.post(reverse('party_answer', args=[room_id]), {'choice': 'A'}, format='json')
        self.client.force_authenticate(user=self.guest)
        self.client.post(reverse('party_answer', args=[room_id]), {'choice': 'B'}, format='json')

        state = self.client.get(reverse('party_state', args=[room_id])).data
        self.assertEqual(state['status'], 'leaderboard')
        reveal = state['reveal']
        self.assertEqual(reveal['distribution'], {'A': 1, 'B': 1, 'C': 0, 'D': 0})
        self.assertEqual(reveal['skipped'], 0)
        self.assertEqual(reveal['review']['explanation'], 'Because A.')
        self.assertEqual(reveal['review']['choices'][0], 'right')
        self.assertFalse(reveal['correct'])  # the guest answered B


class PartyJeopardyModeTests(APITestCase):
    """Final Jeopardy: the betting phase and how the last question settles."""

    def setUp(self):
        self.host = User.objects.create_user(username='jhost', password='x')
        Profile.objects.create(user=self.host)
        self.guest = User.objects.create_user(username='jguest', password='x')
        Profile.objects.create(user=self.guest)
        for i in range(6):
            Question.objects.create(
                question=f'JQ{i}?', choice_a='a', choice_b='b', choice_c='c', choice_d='d',
                answer='A', difficulty=3, question_type='Transitions',
            )

    def _room(self, questions=2):
        self.client.force_authenticate(user=self.host)
        data = self.client.post(reverse('party_create'), {
            'mode': 'jeopardy', 'num_questions': questions, 'seconds_per_question': 60,
            'subject': 'english', 'difficulty': 'medium',
        }, format='json').data
        self.client.force_authenticate(user=self.guest)
        self.client.post(reverse('party_join'), {'code': data['code']}, format='json')
        self.client.force_authenticate(user=self.host)
        self.client.post(reverse('party_start', args=[data['id']]))
        return data['id']

    def _to_question(self, room_id):
        from api.models import PartyRoom
        room = PartyRoom.objects.get(id=room_id)
        room.status = 'question'
        room.phase_started_at = timezone.now()
        room.save()
        return room

    def test_one_question_jeopardy_is_rejected_at_creation(self):
        self.client.force_authenticate(user=self.host)
        data = self.client.post(reverse('party_create'), {
            'mode': 'jeopardy', 'num_questions': 1, 'subject': 'english',
        }, format='json').data
        from api.models import PartyRoom
        self.assertEqual(PartyRoom.objects.get(id=data['id']).num_questions, 2)

    def test_advancing_into_the_last_question_opens_betting(self):
        from api.models import PartyRoom
        room_id = self._room(questions=2)
        self._to_question(room_id)
        self.client.post(reverse('party_answer', args=[room_id]), {'choice': 'A'}, format='json')
        self.client.force_authenticate(user=self.guest)
        self.client.post(reverse('party_answer', args=[room_id]), {'choice': 'A'}, format='json')

        self.client.force_authenticate(user=self.host)
        resp = self.client.post(reverse('party_next', args=[room_id]))
        self.assertEqual(resp.data['status'], 'wager')
        self.assertEqual(PartyRoom.objects.get(id=room_id).status, 'wager')

    def test_classic_mode_never_opens_betting(self):
        self.client.force_authenticate(user=self.host)
        data = self.client.post(reverse('party_create'), {
            'mode': 'classic', 'num_questions': 2, 'subject': 'english',
        }, format='json').data
        self.client.post(reverse('party_start', args=[data['id']]))
        self._to_question(data['id'])
        self.client.post(reverse('party_answer', args=[data['id']]), {'choice': 'A'}, format='json')
        resp = self.client.post(reverse('party_next', args=[data['id']]))
        self.assertEqual(resp.data['status'], 'question')

    def _into_betting(self, room_id):
        from api.models import PartyRoom
        room = PartyRoom.objects.get(id=room_id)
        room.current_index = len(room.question_ids) - 1
        room.status = 'wager'
        room.phase_started_at = timezone.now()
        room.save()
        return room

    def test_bet_is_clamped_to_what_you_actually_have(self):
        from api.models import PartyPlayer, PartyRoom
        room_id = self._room()
        room = self._into_betting(room_id)
        PartyPlayer.objects.filter(room=room, user=self.host).update(score=500)

        self.client.force_authenticate(user=self.host)
        resp = self.client.post(reverse('party_wager', args=[room_id]), {'amount': 9999}, format='json')
        self.assertEqual(resp.data['wager'], 500)  # capped at score, never more

    def test_negative_bet_floors_at_zero(self):
        from api.models import PartyPlayer
        room_id = self._room()
        room = self._into_betting(room_id)
        PartyPlayer.objects.filter(room=room, user=self.host).update(score=500)
        self.client.force_authenticate(user=self.host)
        resp = self.client.post(reverse('party_wager', args=[room_id]), {'amount': -300}, format='json')
        self.assertEqual(resp.data['wager'], 0)

    def test_bets_cannot_be_changed_once_locked(self):
        from api.models import PartyPlayer
        room_id = self._room()
        room = self._into_betting(room_id)
        PartyPlayer.objects.filter(room=room, user=self.host).update(score=500)
        self.client.force_authenticate(user=self.host)
        self.client.post(reverse('party_wager', args=[room_id]), {'amount': 100}, format='json')
        again = self.client.post(reverse('party_wager', args=[room_id]), {'amount': 500}, format='json')
        self.assertEqual(again.status_code, 400)
        self.assertEqual(PartyPlayer.objects.get(room=room, user=self.host).wager, 100)

    def test_everyone_betting_closes_the_phase(self):
        from api.models import PartyPlayer, PartyRoom
        room_id = self._room()
        room = self._into_betting(room_id)
        PartyPlayer.objects.filter(room=room).update(score=400)
        for user in (self.host, self.guest):
            self.client.force_authenticate(user=user)
            self.client.post(reverse('party_wager', args=[room_id]), {'amount': 200}, format='json')
        self.assertEqual(PartyRoom.objects.get(id=room_id).status, 'question')

    def test_correct_final_answer_doubles_the_bet(self):
        from api.models import PartyPlayer
        room_id = self._room()
        room = self._into_betting(room_id)
        PartyPlayer.objects.filter(room=room).update(score=600)
        for user in (self.host, self.guest):
            self.client.force_authenticate(user=user)
            self.client.post(reverse('party_wager', args=[room_id]), {'amount': 250}, format='json')

        self.client.force_authenticate(user=self.host)
        self.client.post(reverse('party_answer', args=[room_id]), {'choice': 'A'}, format='json')
        self.assertEqual(PartyPlayer.objects.get(room=room, user=self.host).score, 850)

    def test_wrong_final_answer_loses_the_bet(self):
        from api.models import PartyPlayer
        room_id = self._room()
        room = self._into_betting(room_id)
        PartyPlayer.objects.filter(room=room).update(score=600)
        for user in (self.host, self.guest):
            self.client.force_authenticate(user=user)
            self.client.post(reverse('party_wager', args=[room_id]), {'amount': 250}, format='json')

        self.client.force_authenticate(user=self.host)
        self.client.post(reverse('party_answer', args=[room_id]), {'choice': 'B'}, format='json')
        self.assertEqual(PartyPlayer.objects.get(room=room, user=self.host).score, 350)

    def test_final_answer_ignores_speed(self):
        """A slow correct answer pays exactly the same as an instant one."""
        from api.models import PartyPlayer, PartyRoom
        room_id = self._room()
        room = self._into_betting(room_id)
        PartyPlayer.objects.filter(room=room).update(score=600)
        for user in (self.host, self.guest):
            self.client.force_authenticate(user=user)
            self.client.post(reverse('party_wager', args=[room_id]), {'amount': 300}, format='json')

        room = PartyRoom.objects.get(id=room_id)
        room.phase_started_at = timezone.now() - timedelta(seconds=55)  # nearly out of time
        room.save(update_fields=['phase_started_at'])
        self.client.force_authenticate(user=self.host)
        self.client.post(reverse('party_answer', args=[room_id]), {'choice': 'A'}, format='json')
        self.assertEqual(PartyPlayer.objects.get(room=room, user=self.host).score, 900)

    def test_sitting_out_the_final_still_costs_the_bet(self):
        """Otherwise not answering would be safer than answering."""
        from api.models import PartyPlayer, PartyRoom
        room_id = self._room()
        room = self._into_betting(room_id)
        PartyPlayer.objects.filter(room=room).update(score=600)
        for user in (self.host, self.guest):
            self.client.force_authenticate(user=user)
            self.client.post(reverse('party_wager', args=[room_id]), {'amount': 400}, format='json')

        # Host answers, guest never does, then the clock runs out.
        self.client.force_authenticate(user=self.host)
        self.client.post(reverse('party_answer', args=[room_id]), {'choice': 'A'}, format='json')
        room = PartyRoom.objects.get(id=room_id)
        room.phase_started_at = timezone.now() - timedelta(seconds=120)
        room.save(update_fields=['phase_started_at'])
        self.client.get(reverse('party_state', args=[room_id]))

        self.assertEqual(PartyRoom.objects.get(id=room_id).status, 'leaderboard')
        self.assertEqual(PartyPlayer.objects.get(room=room, user=self.host).score, 1000)
        self.assertEqual(PartyPlayer.objects.get(room=room, user=self.guest).score, 200)

    def test_a_broke_player_never_blocks_the_betting_phase(self):
        from api.models import PartyPlayer, PartyRoom
        room_id = self._room()
        room = self._into_betting(room_id)
        PartyPlayer.objects.filter(room=room, user=self.host).update(score=500)
        PartyPlayer.objects.filter(room=room, user=self.guest).update(score=0)
        self.client.force_authenticate(user=self.host)
        self.client.post(reverse('party_wager', args=[room_id]), {'amount': 100}, format='json')
        self.assertEqual(PartyRoom.objects.get(id=room_id).status, 'question')

    def test_state_exposes_the_betting_phase(self):
        from api.models import PartyPlayer
        room_id = self._room()
        room = self._into_betting(room_id)
        PartyPlayer.objects.filter(room=room, user=self.host).update(score=700)
        self.client.force_authenticate(user=self.host)
        state = self.client.get(reverse('party_state', args=[room_id])).data
        self.assertEqual(state['status'], 'wager')
        self.assertEqual(state['wager']['your_score'], 700)
        self.assertFalse(state['wager']['locked'])
        self.assertIsNone(state['wager']['your_wager'])


class PartySurvivalModeTests(APITestCase):
    """Survival: hearts, elimination, and the two ways a game ends."""

    def setUp(self):
        self.users = []
        for i in range(3):
            user = User.objects.create_user(username=f's{i}', password='x')
            Profile.objects.create(user=user)
            self.users.append(user)
        self.host = self.users[0]
        for i in range(10):
            Question.objects.create(
                question=f'SQ{i}?', choice_a='a', choice_b='b', choice_c='c', choice_d='d',
                answer='A', difficulty=3, question_type='Transitions',
            )

    def _room(self, joiners=2, **overrides):
        self.client.force_authenticate(user=self.host)
        payload = {'mode': 'survival', 'num_questions': 9, 'seconds_per_question': 60,
                   'subject': 'english', 'difficulty': 'medium', **overrides}
        data = self.client.post(reverse('party_create'), payload, format='json').data
        for user in self.users[1:joiners + 1]:
            self.client.force_authenticate(user=user)
            self.client.post(reverse('party_join'), {'code': data['code']}, format='json')
        self.client.force_authenticate(user=self.host)
        self.client.post(reverse('party_start', args=[data['id']]))
        return data['id']

    def _to_question(self, room_id):
        from api.models import PartyRoom
        room = PartyRoom.objects.get(id=room_id)
        room.status = 'question'
        room.phase_started_at = timezone.now()
        room.save()
        return room

    def _lives(self, room_id, user):
        from api.models import PartyPlayer
        return PartyPlayer.objects.get(room_id=room_id, user=user).lives

    def test_lives_cap_for_last_standing_is_five(self):
        from api.models import PartyRoom
        room_id = self._room(lives=99, last_standing=True)
        self.assertEqual(PartyRoom.objects.get(id=room_id).lives, 5)

    def test_fixed_game_caps_lives_at_sqrt_of_question_count(self):
        from api.models import PartyRoom
        # 9 questions -> ceil(sqrt(9)) == 3
        room_id = self._room(lives=99, last_standing=False)
        self.assertEqual(PartyRoom.objects.get(id=room_id).lives, 3)

    def test_everyone_starts_with_the_room_lives(self):
        room_id = self._room(lives=4, last_standing=True)
        for user in self.users:
            self.assertEqual(self._lives(room_id, user), 4)

    def test_wrong_answer_costs_a_heart(self):
        room_id = self._room(lives=3)
        self._to_question(room_id)
        self.client.force_authenticate(user=self.host)
        self.client.post(reverse('party_answer', args=[room_id]), {'choice': 'B'}, format='json')
        self.assertEqual(self._lives(room_id, self.host), 2)

    def test_correct_answer_keeps_every_heart(self):
        room_id = self._room(lives=3)
        self._to_question(room_id)
        self.client.force_authenticate(user=self.host)
        self.client.post(reverse('party_answer', args=[room_id]), {'choice': 'A'}, format='json')
        self.assertEqual(self._lives(room_id, self.host), 3)

    def test_timing_out_costs_a_heart_too(self):
        """Otherwise waiting the clock out would dodge the risk."""
        from api.models import PartyRoom
        room_id = self._room(lives=3)
        room = self._to_question(room_id)
        self.client.force_authenticate(user=self.host)
        self.client.post(reverse('party_answer', args=[room_id]), {'choice': 'A'}, format='json')

        room = PartyRoom.objects.get(id=room_id)
        room.phase_started_at = timezone.now() - timedelta(seconds=120)
        room.save(update_fields=['phase_started_at'])
        self.client.get(reverse('party_state', args=[room_id]))

        self.assertEqual(self._lives(room_id, self.host), 3)       # answered correctly
        self.assertEqual(self._lives(room_id, self.users[1]), 2)   # never answered
        self.assertEqual(self._lives(room_id, self.users[2]), 2)

    def test_eliminated_player_cannot_answer(self):
        from api.models import PartyPlayer
        room_id = self._room(lives=3, last_standing=True)
        self._to_question(room_id)
        PartyPlayer.objects.filter(room_id=room_id, user=self.host).update(lives=0)
        self.client.force_authenticate(user=self.host)
        resp = self.client.post(reverse('party_answer', args=[room_id]), {'choice': 'A'}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn("out", resp.data['error'])

    def test_last_standing_ends_when_one_player_remains(self):
        from api.models import PartyPlayer, PartyRoom
        room_id = self._room(lives=1, last_standing=True)
        self._to_question(room_id)
        PartyPlayer.objects.filter(room_id=room_id, user__in=self.users[1:]).update(lives=0)

        self.client.force_authenticate(user=self.host)
        self.client.post(reverse('party_answer', args=[room_id]), {'choice': 'A'}, format='json')
        state = self.client.get(reverse('party_state', args=[room_id])).data
        self.assertEqual(state['status'], 'leaderboard')
        self.assertTrue(state['reveal']['is_last'])  # podium next, not another question

        resp = self.client.post(reverse('party_next', args=[room_id]))
        self.assertEqual(resp.data['status'], 'finished')
        self.assertEqual(PartyRoom.objects.get(id=room_id).status, 'finished')

    def test_fixed_game_runs_all_questions_even_at_zero_hearts(self):
        from api.models import PartyPlayer, PartyRoom
        room_id = self._room(lives=1, last_standing=False)
        self._to_question(room_id)
        PartyPlayer.objects.filter(room_id=room_id, user__in=self.users[1:]).update(lives=0)

        self.client.force_authenticate(user=self.host)
        self.client.post(reverse('party_answer', args=[room_id]), {'choice': 'A'}, format='json')
        room = PartyRoom.objects.get(id=room_id)
        room.phase_started_at = timezone.now() - timedelta(seconds=120)
        room.save(update_fields=['phase_started_at'])
        self.client.get(reverse('party_state', args=[room_id]))

        resp = self.client.post(reverse('party_next', args=[room_id]))
        self.assertEqual(resp.data['status'], 'question')  # game continues

    def test_a_zero_heart_player_can_still_answer_in_a_fixed_game(self):
        from api.models import PartyPlayer
        room_id = self._room(lives=1, last_standing=False)
        self._to_question(room_id)
        PartyPlayer.objects.filter(room_id=room_id, user=self.host).update(lives=0)
        self.client.force_authenticate(user=self.host)
        resp = self.client.post(reverse('party_answer', args=[room_id]), {'choice': 'A'}, format='json')
        self.assertEqual(resp.status_code, 200)

    def test_hearts_never_go_negative(self):
        from api.models import PartyPlayer
        room_id = self._room(lives=1, last_standing=False)
        self._to_question(room_id)
        PartyPlayer.objects.filter(room_id=room_id, user=self.host).update(lives=0)
        self.client.force_authenticate(user=self.host)
        self.client.post(reverse('party_answer', args=[room_id]), {'choice': 'B'}, format='json')
        self.assertEqual(self._lives(room_id, self.host), 0)

    def test_standings_rank_by_hearts_then_score(self):
        from api.models import PartyPlayer
        room_id = self._room(lives=3)
        PartyPlayer.objects.filter(room_id=room_id, user=self.host).update(lives=1, score=9000)
        PartyPlayer.objects.filter(room_id=room_id, user=self.users[1]).update(lives=3, score=100)
        PartyPlayer.objects.filter(room_id=room_id, user=self.users[2]).update(lives=3, score=500)

        self.client.force_authenticate(user=self.host)
        state = self.client.get(reverse('party_state', args=[room_id])).data
        # Hearts first, so the 9000-point player with one heart ranks last.
        self.assertEqual([p['username'] for p in state['players']], ['s2', 's1', 's0'])

    def test_eliminated_players_do_not_stall_the_room(self):
        """The question should close once the survivors have all answered."""
        from api.models import PartyPlayer, PartyRoom
        room_id = self._room(lives=2, last_standing=True)
        self._to_question(room_id)
        PartyPlayer.objects.filter(room_id=room_id, user=self.users[2]).update(lives=0)

        for user in self.users[:2]:
            self.client.force_authenticate(user=user)
            self.client.post(reverse('party_answer', args=[room_id]), {'choice': 'A'}, format='json')
        self.assertEqual(PartyRoom.objects.get(id=room_id).status, 'leaderboard')

    def test_state_reports_hearts_and_survivor_count(self):
        room_id = self._room(lives=3, last_standing=True)
        self.client.force_authenticate(user=self.host)
        state = self.client.get(reverse('party_state', args=[room_id])).data
        self.assertEqual(state['your_lives'], 3)
        self.assertFalse(state['you_are_out'])
        self.assertEqual(state['survivors'], 3)
        self.assertEqual(state['settings']['lives'], 3)
        self.assertTrue(state['settings']['last_standing'])


class PartyGoldRushModeTests(APITestCase):
    def setUp(self):
        from api.models import PartyRoom
        self.PartyRoom = PartyRoom
        self.host = User.objects.create_user(username='host', password='x')
        Profile.objects.create(user=self.host)
        self.guest = User.objects.create_user(username='guest', password='x')
        Profile.objects.create(user=self.guest)
        # Enough questions for the 30-question free pool, all answer 'A'.
        for i in range(40):
            Question.objects.create(
                question=f'Q{i}?', choice_a='a', choice_b='b', choice_c='c', choice_d='d',
                answer='A', difficulty=(i % 3) + 2, question_type='Transitions',
            )

    def _play(self):
        """Create a Gold Rush room, seat the guest, start, and reach 'playing'."""
        self.client.force_authenticate(user=self.host)
        data = self.client.post(reverse('party_create'), {
            'mode': 'goldrush', 'subject': 'english', 'difficulty': 'medium', 'time_limit': 600,
        }, format='json').data
        room_id = data['id']
        self.client.force_authenticate(user=self.guest)
        self.client.post(reverse('party_join'), {'code': data['code']}, format='json')
        self.client.force_authenticate(user=self.host)
        self.client.post(reverse('party_start', args=[room_id]))
        room = self.PartyRoom.objects.get(id=room_id)
        room.phase_started_at -= timedelta(seconds=10)  # burn the countdown
        room.save()
        return room_id

    def test_pool_is_sampled_and_capped_for_free_tier(self):
        room_id = self._play()
        room = self.PartyRoom.objects.get(id=room_id)
        self.assertEqual(len(room.question_ids), 30)  # free pool
        # Each player walks their own shuffle of the same pool.
        decks = [p.gq_deck for p in room.players.all()]
        self.assertTrue(all(sorted(d) == sorted(room.question_ids) for d in decks))

    def test_state_serves_a_question_while_playing(self):
        room_id = self._play()
        self.client.force_authenticate(user=self.host)
        state = self.client.get(reverse('party_state', args=[room_id])).data
        self.assertEqual(state['status'], 'playing')
        self.assertEqual(state['gold']['phase'], 'question')
        self.assertEqual(len(state['gold']['question']['choices']), 4)
        self.assertNotIn('answer', state['gold']['question'])
        self.assertGreater(state['seconds_left'], 0)

    def test_wrong_answer_locks_out_then_self_heals(self):
        room_id = self._play()
        self.client.force_authenticate(user=self.host)
        resp = self.client.post(reverse('party_gold_answer', args=[room_id]), {'choice': 'B'}, format='json')
        self.assertFalse(resp.data['correct'])
        self.assertEqual(resp.data['correct_choice'], 'A')
        attempt = self.PartyRoom.objects.get(id=room_id).players.get(user=self.host).answers['g0']
        self.assertFalse(attempt['correct'])
        self.assertIn('question_id', attempt)

        state = self.client.get(reverse('party_state', args=[room_id])).data
        self.assertEqual(state['gold']['phase'], 'wrong')
        # Answering is refused during the penalty.
        blocked = self.client.post(reverse('party_gold_answer', args=[room_id]), {'choice': 'A'}, format='json')
        self.assertEqual(blocked.status_code, 400)

        # Rewind the lockout: the next poll advances to a fresh question.
        room = self.PartyRoom.objects.get(id=room_id)
        player = room.players.get(user=self.host)
        player.gq_locked_until -= timedelta(seconds=5)
        player.save()
        state = self.client.get(reverse('party_state', args=[room_id])).data
        self.assertEqual(state['gold']['phase'], 'question')

    @patch('api.views.party_views._roll_reward', return_value={'kind': 'gold', 'amount': 300})
    def test_correct_answer_opens_a_gold_chest(self, _roll):
        room_id = self._play()
        self.client.force_authenticate(user=self.host)
        resp = self.client.post(reverse('party_gold_answer', args=[room_id]), {'choice': 'A'}, format='json')
        self.assertTrue(resp.data['correct'])

        state = self.client.get(reverse('party_state', args=[room_id])).data
        self.assertEqual(state['gold']['phase'], 'chest')
        self.assertEqual(state['gold']['chest_count'], 3)

        opened = self.client.post(reverse('party_gold_chest', args=[room_id]), {'pick': 0}, format='json')
        self.assertEqual(opened.data['kind'], 'gold')
        self.assertEqual(opened.data['gold'], 300)
        # Chest cleared, back to a question.
        state = self.client.get(reverse('party_state', args=[room_id])).data
        self.assertEqual(state['gold']['phase'], 'question')

    @patch('api.views.party_views._roll_reward', return_value={'kind': 'steal', 'pct': 50})
    def test_steal_takes_half_of_a_chosen_player(self, _roll):
        room_id = self._play()
        room = self.PartyRoom.objects.get(id=room_id)
        room.players.filter(user=self.guest).update(score=1000)

        self.client.force_authenticate(user=self.host)
        self.client.post(reverse('party_gold_answer', args=[room_id]), {'choice': 'A'}, format='json')
        # First open reports it needs a target.
        step = self.client.post(reverse('party_gold_chest', args=[room_id]), {'pick': 0}, format='json')
        self.assertTrue(step.data['needs_target'])
        self.assertEqual(step.data['kind'], 'steal')
        # Then name the victim.
        done = self.client.post(reverse('party_gold_chest', args=[room_id]),
                                {'target': self.guest.id}, format='json')
        self.assertEqual(done.data['amount'], 500)
        self.assertEqual(done.data['gold'], 500)
        self.assertEqual(room.players.get(user=self.guest).score, 500)

    @patch('api.views.party_views._roll_reward', return_value={'kind': 'swap'})
    def test_swap_trades_totals(self, _roll):
        room_id = self._play()
        room = self.PartyRoom.objects.get(id=room_id)
        room.players.filter(user=self.host).update(score=100)
        room.players.filter(user=self.guest).update(score=900)

        self.client.force_authenticate(user=self.host)
        self.client.post(reverse('party_gold_answer', args=[room_id]), {'choice': 'A'}, format='json')
        self.client.post(reverse('party_gold_chest', args=[room_id]), {'pick': 0}, format='json')
        done = self.client.post(reverse('party_gold_chest', args=[room_id]),
                                {'target': self.guest.id}, format='json')
        self.assertEqual(done.data['gold'], 900)
        self.assertEqual(room.players.get(user=self.host).score, 900)
        self.assertEqual(room.players.get(user=self.guest).score, 100)

    def test_clock_running_out_finishes_the_game(self):
        room_id = self._play()
        room = self.PartyRoom.objects.get(id=room_id)
        room.phase_started_at -= timedelta(seconds=room.time_limit + 1)
        room.save()
        self.client.force_authenticate(user=self.host)
        state = self.client.get(reverse('party_state', args=[room_id])).data
        self.assertEqual(state['status'], 'finished')
        # Answers are refused once the clock is done.
        resp = self.client.post(reverse('party_gold_answer', args=[room_id]), {'choice': 'A'}, format='json')
        self.assertEqual(resp.status_code, 400)


class PracticeTestResultTests(APITestCase):
    """Save + history for full-length practice tests."""

    def setUp(self):
        from api.models import PracticeTestResult
        self.PracticeTestResult = PracticeTestResult
        self.user = User.objects.create_user(username='tester', password='x')
        self.client.force_authenticate(user=self.user)

    def _save(self, score, **extra):
        payload = {
            'score': score, 'correct': 7, 'total': 10,
            'test_id': 1, 'test_name': 'SAT Diagnostic Test',
            'time_used_seconds': 900,
            'questions': [
                {'question_id': 5, 'user_choice': 'A', 'correct': True},
                {'question_id': 6, 'user_choice': None, 'correct': False},
            ],
            **extra,
        }
        return self.client.post(reverse('save_test_result'), payload, format='json')

    def test_save_requires_auth(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(self._save(500).status_code, 401)

    def test_save_rejects_missing_score(self):
        resp = self.client.post(reverse('save_test_result'), {'correct': 1, 'total': 2}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_save_returns_previous_scores(self):
        first = self._save(500)
        self.assertEqual(first.status_code, 200)
        self.assertIsNone(first.data['previous_best'])
        self.assertIsNone(first.data['previous_last'])

        second = self._save(540)
        self.assertEqual(second.data['previous_best'], 500)
        self.assertEqual(second.data['previous_last'], 500)

        third = self._save(520)
        self.assertEqual(third.data['previous_best'], 540)
        self.assertEqual(third.data['previous_last'], 540)

        saved = self.PracticeTestResult.objects.filter(user=self.user)
        self.assertEqual(saved.count(), 3)
        self.assertEqual(saved.first().questions[0]['question_id'], 5)

    def test_history_summary(self):
        empty = self.client.get(reverse('practice_test_history'))
        self.assertEqual(empty.data['tests_taken'], 0)
        self.assertIsNone(empty.data['best_score'])

        for score in (500, 560, 530):
            self._save(score)
        resp = self.client.get(reverse('practice_test_history'))
        self.assertEqual(resp.data['tests_taken'], 3)
        self.assertEqual(resp.data['best_score'], 560)
        self.assertEqual(resp.data['average_score'], 530)
        self.assertEqual(resp.data['latest_score'], 530)
        # Newest first, with per-question data intact for the review page.
        self.assertEqual([r['score'] for r in resp.data['results']], [530, 560, 500])
        self.assertEqual(resp.data['results'][0]['questions'][1]['user_choice'], None)

    def test_history_is_per_user(self):
        self._save(500)
        other = User.objects.create_user(username='other', password='x')
        self.client.force_authenticate(user=other)
        resp = self.client.get(reverse('practice_test_history'))
        self.assertEqual(resp.data['tests_taken'], 0)


class MarketingSyncTests(TransactionTestCase):
    """Resend audience sync: subscription mirroring, signals, and webhook."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='mktg', email='mktg@example.com',
            first_name='Mk', last_name='Tg',
        )
        self.profile = Profile.objects.create(user=self.user)

    def _verify_email(self):
        EmailAddress.objects.create(
            user=self.user, email=self.user.email, verified=True, primary=True,
        )

    def _set_opt_in(self, value):
        # Bypass the post_save signal so seeding does not make a network call.
        Profile.objects.filter(pk=self.profile.pk).update(marketing_opt_in=value)
        self.profile.refresh_from_db()

    @override_settings(RESEND_API_KEY='re_test')
    def test_opted_in_unverified_syncs_as_unsubscribed(self):
        from api import marketing
        self._set_opt_in(True)
        with patch.object(marketing.requests, 'patch') as mock_patch:
            mock_patch.return_value = SimpleNamespace(
                status_code=200, raise_for_status=lambda: None,
            )
            marketing.sync_marketing_contact(self.user)
        # No verified email yet -> suppressed.
        self.assertTrue(mock_patch.call_args.kwargs['json']['unsubscribed'])

    @override_settings(RESEND_API_KEY='re_test')
    def test_opted_in_verified_syncs_as_subscribed(self):
        from api import marketing
        self._set_opt_in(True)
        self._verify_email()
        with patch.object(marketing.requests, 'patch') as mock_patch:
            mock_patch.return_value = SimpleNamespace(
                status_code=200, raise_for_status=lambda: None,
            )
            marketing.sync_marketing_contact(self.user)
        self.assertFalse(mock_patch.call_args.kwargs['json']['unsubscribed'])

    @override_settings(RESEND_API_KEY='re_test')
    def test_upsert_falls_back_to_post_on_404(self):
        from api import marketing
        with patch.object(marketing.requests, 'patch') as mock_patch, \
                patch.object(marketing.requests, 'post') as mock_post:
            mock_patch.return_value = SimpleNamespace(
                status_code=404, raise_for_status=lambda: None,
            )
            mock_post.return_value = SimpleNamespace(
                status_code=201, raise_for_status=lambda: None,
            )
            ok = marketing.sync_marketing_contact(self.user)
        self.assertTrue(ok)
        self.assertTrue(mock_post.called)

    @override_settings(RESEND_API_KEY='re_test')
    def test_signal_syncs_on_opt_in_change_only(self):
        with patch('api.signals.sync_marketing_contact') as mock_sync:
            # Unrelated field save must NOT hit Resend.
            self.profile.elo_rating = 1300
            self.profile.save(update_fields=['elo_rating'])
            self.assertFalse(mock_sync.called)
            # Opt-in change must sync.
            self.profile.marketing_opt_in = True
            self.profile.save(update_fields=['marketing_opt_in'])
            self.assertTrue(mock_sync.called)

    @override_settings(RESEND_API_KEY='re_test')
    def test_webhook_unsubscribe_flips_flag(self):
        import base64 as _b64
        import hashlib as _hl
        import hmac as _hm
        import json as _json

        self._set_opt_in(True)

        secret = 'whsec_' + _b64.b64encode(b'0123456789abcdef').decode()
        body = _json.dumps({
            'type': 'contact.updated',
            'data': {'email': 'mktg@example.com', 'unsubscribed': True},
        }).encode()
        svix_id, svix_ts = 'msg_1', '1614265330'
        signed = f'{svix_id}.{svix_ts}.'.encode() + body
        sig = _b64.b64encode(_hm.new(
            _b64.b64decode(secret[len('whsec_'):]), signed, _hl.sha256,
        ).digest()).decode()

        with override_settings(RESEND_WEBHOOK_SECRET=secret):
            resp = self.client.post(
                reverse('resend_webhook'), data=body,
                content_type='application/json',
                HTTP_SVIX_ID=svix_id, HTTP_SVIX_TIMESTAMP=svix_ts,
                HTTP_SVIX_SIGNATURE=f'v1,{sig}',
            )
        self.assertEqual(resp.status_code, 200)
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.marketing_opt_in)

    @override_settings(RESEND_WEBHOOK_SECRET='whsec_' + 'YWJjZGVmZ2hpamtsbW5vcA==')
    def test_webhook_rejects_bad_signature(self):
        resp = self.client.post(
            reverse('resend_webhook'),
            data=b'{"type": "contact.updated", "data": {}}',
            content_type='application/json',
            HTTP_SVIX_ID='msg_1', HTTP_SVIX_TIMESTAMP='1',
            HTTP_SVIX_SIGNATURE='v1,deadbeef',
        )
        self.assertEqual(resp.status_code, 400)


class ImportDuplicateTests(APITestCase):
    """The import page's duplicate check: a re-parse of an item already in the
    bank must be caught through cosmetic differences, while two questions from
    the same template with different numbers must not be."""

    QUESTION = "The graph of $f$ is shown. [svg]<svg viewBox='0 0 10 10'><path d='M0 0'/></svg>[/svg]\nWhat is $f(3)$?"

    def setUp(self):
        self.admin = User.objects.create_user(username='dupe-admin', is_staff=True)
        self.client.force_authenticate(user=self.admin)
        self.existing = Question.objects.create(
            question=self.QUESTION, choice_a='1', choice_b='2', choice_c='3', choice_d='4',
            answer='B', difficulty=3, question_type='Command of Evidence',
        )

    def draft(self, **overrides):
        draft = {
            'question': self.QUESTION, 'choice_a': '1', 'choice_b': '2', 'choice_c': '3', 'choice_d': '4',
            'answer': 'B', 'difficulty': 3, 'question_type': 'Command of Evidence', 'explanation': '',
        }
        draft.update(overrides)
        return draft

    def post(self, drafts):
        resp = self.client.post(reverse('generation_duplicates'), {'questions': drafts}, format='json')
        self.assertEqual(resp.status_code, 200)
        return resp.data['duplicates']

    def test_reparsed_question_matches_through_cosmetic_differences(self):
        reparsed = self.draft(question=(
            "The graph of \\(f\\) is shown.  [svg]<svg viewBox='0 0 20 20'><circle cx='2'/></svg>[/svg] "
            "What is \\(f(3)\\)?"
        ))
        duplicates = self.post([reparsed])
        self.assertEqual(duplicates[0]['question_id'], self.existing.id)
        self.assertEqual(duplicates[0]['comparison']['choice_b'], '2')

    def test_table_representation_is_ignored(self):
        stored = Question.objects.create(
            question=(
                'Results\n$$\\begin{array}{|c|c|} \\hline \\text{Year} & \\text{Value} '
                '\\\\ \\hline 2024 & 17 \\\\ \\hline \\end{array}$$\nWhich choice is supported?'
            ),
            choice_a='First', choice_b='Second', choice_c='Third', choice_d='Fourth',
            answer='A', difficulty=2, question_type='Command of Evidence',
        )
        reparsed = self.draft(
            question='Results\n<table><tr><td>Different markup</td></tr></table>\nWhich choice is supported?',
            choice_a='First', choice_b='Second', choice_c='Third', choice_d='Fourth',
        )
        self.assertEqual(self.post([reparsed])[0]['question_id'], stored.id)

    def test_same_template_with_different_numbers_is_not_a_duplicate(self):
        self.assertEqual(self.post([self.draft(question=self.QUESTION.replace('f(3)', 'f(5)'))]), {})
        self.assertEqual(self.post([self.draft(choice_d='5')]), {})

    def test_repeat_inside_the_batch_is_flagged_once(self):
        new = self.draft(question='A brand new question?')
        duplicates = self.post([new, new])
        self.assertNotIn(0, duplicates)
        self.assertEqual(duplicates[1]['where'], 'batch')
        self.assertEqual(duplicates[1]['draft_index'], 0)

    def test_exact_match_stays_inside_one_question_type(self):
        self.assertEqual(self.post([self.draft(question_type='Central Ideas and Details')]), {})

    PASSAGE = (
        'The following text is adapted from a 1915 essay. Weavers in the region had for generations '
        'treated the loom as a communal instrument, one whose output belonged less to any single '
        'household than to the village that maintained it. When the first mechanized frames arrived, '
        'observers assumed the older arrangement would collapse within a season, and yet the ledgers '
        'kept by the guild suggest something stranger: the communal claim survived, quietly rewritten '
        'around the new machines rather than abandoned in the face of them, so that ownership and use '
        'drifted apart for the better part of two decades before either was settled.\n\n'
        'Which choice best states the main idea of the text?'
    )

    def test_english_passage_is_caught_when_a_reparse_rewords_it(self):
        stored = Question.objects.create(
            question=self.PASSAGE, choice_a='w', choice_b='x', choice_c='y', choice_d='z',
            answer='A', difficulty=3, question_type='Central Ideas and Details',
        )
        reworded = self.draft(
            question=self.PASSAGE.replace('best states', 'best describes').replace('1915 essay', '1915 essay.'),
            choice_a='w', choice_b='x', choice_c='y', choice_d='z',
            question_type='Central Ideas and Details',
        )
        duplicate = self.post([reworded])[0]
        self.assertEqual(duplicate['question_id'], stored.id)
        self.assertEqual(duplicate['match'], 'near')

    def test_near_stem_with_a_materially_different_choice_is_not_flagged(self):
        Question.objects.create(
            question=self.PASSAGE, choice_a='w', choice_b='x', choice_c='y', choice_d='z',
            answer='A', difficulty=3, question_type='Central Ideas and Details',
        )
        variant = self.draft(
            question=self.PASSAGE.replace('best states', 'best describes'),
            choice_a='w', choice_b='x', choice_c='y', choice_d='an entirely different final choice',
            question_type='Central Ideas and Details',
        )
        self.assertEqual(self.post([variant]), {})

        all_new_choices = self.draft(
            question=self.PASSAGE.replace('best states', 'best describes'),
            choice_a='first new choice', choice_b='second new choice',
            choice_c='third new choice', choice_d='fourth new choice',
            question_type='Central Ideas and Details',
        )
        self.assertEqual(self.post([all_new_choices]), {})

    def test_english_near_match_stays_inside_one_skill(self):
        Question.objects.create(
            question=self.PASSAGE, choice_a='w', choice_b='x', choice_c='y', choice_d='z',
            answer='A', difficulty=3, question_type='Central Ideas and Details',
        )
        other_skill = self.draft(question=self.PASSAGE, choice_a='w', choice_b='x', choice_c='y',
                                 choice_d='a different choice', question_type='Text Structure and Purpose')
        self.assertEqual(self.post([other_skill]), {})

    def test_math_is_not_checked(self):
        stem = ('A scientist models the population of a colony over time. The model assumes the colony '
                'grows by a fixed percentage each year and that no individuals leave the colony. '
                'The population was %s in the first year of the study and the annual growth rate is %s. '
                'Which equation gives the population $p$ after $t$ years?')
        Question.objects.create(
            question=stem % ('$15{,}000$', '$3\\%$'), choice_a='1', choice_b='2', choice_c='3', choice_d='4',
            answer='A', difficulty=3, question_type='Nonlinear functions',
        )
        twin = self.draft(question=stem % ('$18{,}000$', '$4\\%$'), question_type='Nonlinear functions')
        self.assertEqual(self.post([twin]), {})
        exact = self.draft(question=stem % ('$15{,}000$', '$3\\%$'), question_type='Nonlinear functions')
        self.assertEqual(self.post([exact]), {})
        legacy_math = Question.objects.create(
            question='Legacy math item', choice_a='1', choice_b='2', choice_c='3', choice_d='4',
            answer='A', difficulty=2, question_type='Math',
        )
        self.assertEqual(self.post([self.draft(
            question=legacy_math.question, question_type='Math',
        )]), {})


class TournamentIntegrityTests(APITestCase):
    """Guards on the competitive path: scoring, ownership, and the clock.

    Each test here maps to something that was exploitable: score inflation by
    resubmission, writing into another player's answer row, answering after the
    round closed, joining outside the tournament window, and anonymous
    edit/delete of a tournament.
    """

    def setUp(self):
        from api.models import Tournament
        self.user = User.objects.create_user('player', 'p@example.com', 'pw')
        self.client.force_authenticate(self.user)
        self.tournament = Tournament.objects.create(
            name='Round', description='d',
            start_time=timezone.now() - timedelta(minutes=5),
            end_time=timezone.now() + timedelta(days=1),
            duration=timedelta(minutes=30),
        )
        self.questions = [
            Question.objects.create(
                question=f'q{i}', choice_a='right', choice_b='b', choice_c='c', choice_d='d',
                answer='A', difficulty=1, explanation=f'because {i}',
            ) for i in range(3)
        ]
        self.tournament.questions.set(self.questions)

    def join(self, client=None):
        client = client or self.client
        client.post(f'/api/tournaments/{self.tournament.id}/join/', {}, format='json')
        return client.post(f'/api/tournaments/{self.tournament.id}/questions/', {}, format='json').data

    def answer(self, row, choice, client=None):
        return (client or self.client).post(
            f'/api/tournaments/{self.tournament.id}/submit-answer/',
            {'tournament_question_id': row['id'], 'selected_choice': choice}, format='json',
        )

    def participation(self, user=None):
        from api.models import TournamentParticipation
        return TournamentParticipation.objects.get(user=user or self.user, tournament=self.tournament)

    def test_resubmitting_one_question_cannot_inflate_the_score(self):
        rows = self.join()
        self.assertEqual(self.answer(rows[0], 'right').status_code, 201)
        for _ in range(5):
            self.assertEqual(self.answer(rows[0], 'right').status_code, 409)
        self.assertEqual(self.participation().score, 1)

    def test_score_never_exceeds_the_question_count(self):
        rows = self.join()
        for row in rows:
            self.answer(row, 'right')
        self.assertEqual(self.participation().score, len(self.questions))

    def test_cannot_write_into_another_players_answer_row(self):
        from api.models import TournamentQuestion
        victim = User.objects.create_user('victim', 'v@example.com', 'pw')
        victim_client = APIClient()
        victim_client.force_authenticate(victim)
        self.join(victim_client)
        victim_row = TournamentQuestion.objects.filter(participation__user=victim).first()

        self.join()
        response = self.answer({'id': victim_row.id}, 'right')

        self.assertEqual(response.status_code, 404)
        victim_row.refresh_from_db()
        self.assertEqual(victim_row.status, 'Blank')

    def test_cannot_answer_after_the_clock_runs_out(self):
        rows = self.join()
        participation = self.participation()
        participation.end_time = timezone.now() - timedelta(minutes=1)
        participation.save(update_fields=['end_time'])

        self.assertEqual(self.answer(rows[0], 'right').status_code, 403)
        self.assertEqual(self.participation().score, 0)

    def test_cannot_answer_after_finishing(self):
        rows = self.join()
        self.client.post(f'/api/tournaments/{self.tournament.id}/finish/', {}, format='json')
        self.assertEqual(self.answer(rows[0], 'right').status_code, 403)

    def test_cannot_join_before_start_or_after_close(self):
        from api.models import Tournament
        upcoming = Tournament.objects.create(
            name='Later', description='d', start_time=timezone.now() + timedelta(days=7),
            end_time=timezone.now() + timedelta(days=8), duration=timedelta(minutes=30))
        closed = Tournament.objects.create(
            name='Over', description='d', start_time=timezone.now() - timedelta(days=8),
            end_time=timezone.now() - timedelta(days=7), duration=timedelta(minutes=30))

        self.assertEqual(self.client.post(f'/api/tournaments/{upcoming.id}/join/', {}, format='json').status_code, 400)
        self.assertEqual(self.client.post(f'/api/tournaments/{closed.id}/join/', {}, format='json').status_code, 400)

    def test_a_run_cannot_outlast_the_tournament(self):
        from api.models import Tournament
        closing_soon = Tournament.objects.create(
            name='Closing', description='d', start_time=timezone.now() - timedelta(minutes=1),
            end_time=timezone.now() + timedelta(minutes=5), duration=timedelta(minutes=30))
        closing_soon.questions.set(self.questions)

        self.client.post(f'/api/tournaments/{closing_soon.id}/join/', {}, format='json')

        self.assertLessEqual(self.participation_for(closing_soon).end_time, closing_soon.end_time)

    def participation_for(self, tournament):
        from api.models import TournamentParticipation
        return TournamentParticipation.objects.get(user=self.user, tournament=tournament)

    def test_anonymous_cannot_edit_or_delete_a_tournament(self):
        from api.models import Tournament
        anon = APIClient()
        self.assertIn(anon.delete(f'/api/tournaments/{self.tournament.id}/').status_code, (403, 405))
        self.assertIn(anon.put(f'/api/tournaments/{self.tournament.id}/', {'name': 'HACKED'},
                               format='json').status_code, (403, 405))
        self.assertTrue(Tournament.objects.filter(id=self.tournament.id).exists())
        self.tournament.refresh_from_db()
        self.assertEqual(self.tournament.name, 'Round')


class TournamentReviewTests(APITestCase):
    """What review needs: the user's own choice, and answers that stay reachable."""

    def setUp(self):
        from api.models import Tournament
        self.user = User.objects.create_user('reviewer', 'r@example.com', 'pw')
        self.client.force_authenticate(self.user)
        self.tournament = Tournament.objects.create(
            name='Round', description='d',
            start_time=timezone.now() - timedelta(minutes=5),
            end_time=timezone.now() + timedelta(days=1),
            duration=timedelta(minutes=30),
        )
        self.question = Question.objects.create(
            question='q', choice_a='right', choice_b='wrong', choice_c='c', choice_d='d',
            answer='A', difficulty=1, explanation='because',
        )
        self.tournament.questions.set([self.question])

    def test_review_records_which_choice_the_user_picked(self):
        self.client.post(f'/api/tournaments/{self.tournament.id}/join/', {}, format='json')
        rows = self.client.post(f'/api/tournaments/{self.tournament.id}/questions/', {}, format='json').data
        self.client.post(f'/api/tournaments/{self.tournament.id}/submit-answer/',
                         {'tournament_question_id': rows[0]['id'], 'selected_choice': 'wrong'}, format='json')
        self.client.post(f'/api/tournaments/{self.tournament.id}/finish/', {}, format='json')

        reviewed = self.client.post(f'/api/tournaments/{self.tournament.id}/questions/', {}, format='json').data
        self.assertEqual(reviewed[0]['status'], 'Incorrect')
        self.assertEqual(reviewed[0]['selected_choice'], 'wrong')

    def test_an_abandoned_run_stops_hiding_answers_once_its_clock_expires(self):
        from api.models import TournamentParticipation
        self.client.post(f'/api/tournaments/{self.tournament.id}/join/', {}, format='json')
        blocked = self.client.post('/api/get_answer/', {'question_id': self.question.id}, format='json')
        self.assertEqual(blocked.status_code, 403)

        # The tab was closed, so nothing ever called finish/ — the row stays Active.
        participation = TournamentParticipation.objects.get(user=self.user, tournament=self.tournament)
        participation.end_time = timezone.now() - timedelta(minutes=1)
        participation.save(update_fields=['end_time'])
        self.assertEqual(participation.status, 'Active')

        allowed = self.client.post('/api/get_answer/', {'question_id': self.question.id}, format='json')
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.data['explanation'], 'because')

    def test_leaderboard_needs_a_login_and_hides_the_grid_from_outsiders(self):
        self.client.post(f'/api/tournaments/{self.tournament.id}/join/', {}, format='json')
        url = f'/api/tournaments/{self.tournament.id}/leaderboard/'

        self.assertEqual(APIClient().get(url).status_code, 401)

        outsider = APIClient()
        outsider.force_authenticate(User.objects.create_user('outsider', 'o@example.com', 'pw'))
        rows = outsider.get(url).data
        self.assertEqual(rows[0]['tournament_questions'], [])
        self.assertIn('score', rows[0])

        own = self.client.get(url).data
        self.assertEqual(len(own[0]['tournament_questions']), 1)
