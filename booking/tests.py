from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse
from datetime import date, time, timedelta
from django.utils import timezone
import time as pytime

from .models import Checkout, Client, ClientDependent, ClientProfile, Reservation, TimeSlot, Trainer, TrainerAvailability, UserTwoFactorAuth
from .services import get_platform_fee_percent
from .views import _send_checkout_confirmation_email, _totp_code, _validate_trainer_coupon


User = get_user_model()


class AccountRoleRoutingTests(TestCase):
    def _create_user(self, *, email: str, password: str = "pass12345"):
        return User.objects.create_user(
            username=email,
            email=email,
            password=password,
        )

    def _attach_trainer(self, user, *, verified=True):
        return Trainer.objects.create(
            user=user,
            business_name="Trainer Pro",
            ath_mobile_handle="@trainer",
            email_verified=verified,
            active=True,
        )

    def _attach_client(self, user):
        return ClientProfile.objects.create(
            user=user,
            full_name="Client User",
            active=True,
        )

    def test_portal_home_with_both_roles_goes_to_mode_select(self):
        user = self._create_user(email="both@example.com")
        self._attach_trainer(user, verified=True)
        self._attach_client(user)
        self.client.force_login(user)

        response = self.client.get(reverse("booking:account_portal_home"))

        self.assertRedirects(response, reverse("booking:account_mode_select"))

    def test_mode_select_sets_role_and_redirects(self):
        user = self._create_user(email="switch@example.com")
        self._attach_trainer(user, verified=True)
        self._attach_client(user)
        self.client.force_login(user)

        response = self.client.post(
            reverse("booking:account_mode_select"),
            {"mode": "client"},
        )

        self.assertRedirects(response, reverse("booking:client_portal_dashboard"))
        session = self.client.session
        self.assertEqual(session.get("account_active_role"), "client")

    def test_login_with_next_client_dashboard_respects_client_target(self):
        user = self._create_user(email="nextclient@example.com")
        # Deliberately unverified trainer + active client profile.
        self._attach_trainer(user, verified=False)
        self._attach_client(user)

        response = self.client.post(
            reverse("login"),
            {
                "username": "nextclient@example.com",
                "password": "pass12345",
                "next": reverse("booking:client_portal_dashboard"),
            },
        )

        self.assertRedirects(response, reverse("booking:client_portal_dashboard"))
        session = self.client.session
        self.assertEqual(session.get("account_active_role"), "client")

    def test_login_with_next_trainer_for_unverified_trainer_goes_verify(self):
        user = self._create_user(email="nexttrainer@example.com")
        self._attach_trainer(user, verified=False)

        response = self.client.post(
            reverse("login"),
            {
                "username": "nexttrainer@example.com",
                "password": "pass12345",
                "next": reverse("booking:trainer_portal"),
            },
        )

        self.assertRedirects(
            response,
            f"{reverse('booking:trainer_verify_pending')}?email=nexttrainer@example.com",
        )

    def test_trainer_portal_without_trainer_profile_redirects_role_management(self):
        user = self._create_user(email="norole@example.com")
        self.client.force_login(user)

        response = self.client.get(reverse("booking:trainer_portal"))

        self.assertRedirects(response, reverse("booking:account_role_management"))


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    TRAINER_SEND_TRANSACTIONAL_EMAILS=True,
)
class CheckoutConfirmationEmailTests(TestCase):
    def test_confirmation_email_sent_once_with_pdf_invoice(self):
        user = User.objects.create_user(username="t@example.com", email="t@example.com", password="pass12345")
        trainer = Trainer.objects.create(
            user=user,
            business_name="Trainer Pro",
            ath_mobile_handle="@trainer",
            email_verified=True,
            active=True,
        )
        client = Client.objects.create(trainer=trainer, name="Client One", email="client@example.com")
        slot = TimeSlot.objects.create(
            trainer=trainer,
            date=date(2026, 2, 25),
            time=time(10, 0),
            capacity=5,
            duration_minutes=60,
            active=True,
        )
        checkout = Checkout.objects.create(
            trainer=trainer,
            client=client,
            payment_method=Checkout.PAYMENT_STRIPE,
            status=Checkout.STATUS_CONFIRMED,
            currency="USD",
            total_amount="45.00",
            stripe_payment_intent_id="pi_test_123",
        )
        Reservation.objects.create(
            trainer=trainer,
            client=client,
            timeslot=slot,
            checkout=checkout,
            amount_due="45.00",
            payment_method=Reservation.PAYMENT_STRIPE,
            paid=True,
        )

        sent_first = _send_checkout_confirmation_email(checkout)
        sent_second = _send_checkout_confirmation_email(checkout)
        checkout.refresh_from_db()

        self.assertTrue(sent_first)
        self.assertFalse(sent_second)
        self.assertIsNotNone(checkout.confirmation_email_sent_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["client@example.com"])
        pdf_attachments = [
            a for a in mail.outbox[0].attachments
            if isinstance(a, tuple) and len(a) == 3 and a[2] == "application/pdf"
        ]
        self.assertEqual(len(pdf_attachments), 1)
        filename, _content, mimetype = pdf_attachments[0]
        self.assertTrue(filename.endswith(".pdf"))
        self.assertEqual(mimetype, "application/pdf")


class CouponDiscountTests(TestCase):
    def test_coupon_applies_discount_to_checkout_and_reservations(self):
        user = User.objects.create_user(username="trainer2@example.com", email="trainer2@example.com", password="pass12345")
        trainer = Trainer.objects.create(
            user=user,
            business_name="Discount Trainer",
            ath_mobile_handle="@ath",
            email_verified=True,
            active=True,
            session_price="100.00",
            discount_code="SAVE10",
            discount_percent_off=10,
        )
        TrainerAvailability.objects.create(
            trainer=trainer,
            weekday=0,
            start_time=time(9, 0),
            end_time=time(10, 0),
            active=True,
        )
        slot = TimeSlot.objects.create(
            trainer=trainer,
            date=timezone.localdate() + timedelta(days=1),
            time=time(9, 0),
            capacity=5,
            duration_minutes=60,
            active=True,
        )

        response = self.client.post(
            reverse("booking:checkout", kwargs={"slug": trainer.slug}) + "?week=current",
            {
                "name": "Client Discount",
                "email": "client.discount@example.com",
                "phone": "",
                "payment_method": "ATH",
                "coupon_code": "SAVE10",
                "timeslot_ids": [str(slot.id)],
            },
        )

        self.assertEqual(response.status_code, 200)
        checkout = Checkout.objects.get(trainer=trainer)
        reservation = Reservation.objects.get(checkout=checkout)
        self.assertEqual(str(checkout.total_amount), "90.00")
        self.assertEqual(checkout.applied_discount_code, "SAVE10")
        self.assertEqual(checkout.applied_discount_percent, 10)
        self.assertEqual(str(checkout.discount_amount), "10.00")
        self.assertEqual(str(reservation.amount_due), "90.00")

    def test_coupon_fails_when_expired(self):
        user = User.objects.create_user(username="trainer3@example.com", email="trainer3@example.com", password="pass12345")
        trainer = Trainer.objects.create(
            user=user,
            business_name="Expired Coupon Trainer",
            ath_mobile_handle="@ath",
            email_verified=True,
            active=True,
            session_price="100.00",
            discount_code="OLD10",
            discount_percent_off=10,
            discount_expires_on=timezone.localdate() - timedelta(days=1),
        )
        ok, percent, msg = _validate_trainer_coupon(trainer=trainer, coupon_code_input="OLD10")
        self.assertFalse(ok)
        self.assertEqual(percent, 0)
        self.assertIn("expir", msg.lower())

    def test_coupon_fails_when_usage_limit_reached(self):
        user = User.objects.create_user(username="trainer4@example.com", email="trainer4@example.com", password="pass12345")
        trainer = Trainer.objects.create(
            user=user,
            business_name="Limited Coupon Trainer",
            ath_mobile_handle="@ath",
            email_verified=True,
            active=True,
            session_price="100.00",
            discount_code="LIMIT10",
            discount_percent_off=10,
            discount_max_uses=1,
        )
        client = Client.objects.create(trainer=trainer, name="Existing", email="existing@example.com")
        Checkout.objects.create(
            trainer=trainer,
            client=client,
            payment_method=Checkout.PAYMENT_ATH,
            status=Checkout.STATUS_CONFIRMED,
            currency="USD",
            total_amount="90.00",
            applied_discount_code="LIMIT10",
            applied_discount_percent=10,
            discount_amount="10.00",
        )
        ok, percent, msg = _validate_trainer_coupon(trainer=trainer, coupon_code_input="LIMIT10")
        self.assertFalse(ok)
        self.assertEqual(percent, 0)
        self.assertIn("límite", msg.lower())

    def test_trainer_fee_override_is_saved_on_checkout(self):
        user = User.objects.create_user(username="trainer5@example.com", email="trainer5@example.com", password="pass12345")
        trainer = Trainer.objects.create(
            user=user,
            business_name="Fee Override Trainer",
            ath_mobile_handle="@ath",
            email_verified=True,
            active=True,
            session_price="100.00",
            platform_fee_percent_override="20.00",
        )
        TrainerAvailability.objects.create(
            trainer=trainer,
            weekday=0,
            start_time=time(8, 0),
            end_time=time(9, 0),
            active=True,
        )
        slot = TimeSlot.objects.create(
            trainer=trainer,
            date=timezone.localdate() + timedelta(days=1),
            time=time(8, 0),
            capacity=5,
            duration_minutes=60,
            active=True,
        )

        response = self.client.post(
            reverse("booking:checkout", kwargs={"slug": trainer.slug}) + "?week=current",
            {
                "name": "Client Fee",
                "email": "fee@example.com",
                "payment_method": "ATH",
                "timeslot_ids": [str(slot.id)],
            },
        )

        self.assertEqual(response.status_code, 200)
        checkout = Checkout.objects.get(trainer=trainer)
        self.assertEqual(str(checkout.total_amount), "100.00")
        self.assertEqual(str(checkout.platform_fee_percent_applied), "20.00")
        self.assertEqual(str(checkout.platform_fee_amount), "20.00")
        self.assertEqual(str(checkout.trainer_net_amount), "80.00")

    @override_settings(PLATFORM_FEE_PERCENT=10)
    def test_default_fee_is_zero_when_trainer_has_no_override(self):
        user = User.objects.create_user(username="trainer6@example.com", email="trainer6@example.com", password="pass12345")
        trainer = Trainer.objects.create(
            user=user,
            business_name="No Override Trainer",
            ath_mobile_handle="@ath",
            email_verified=True,
            active=True,
            session_price="100.00",
            platform_fee_percent_override=None,
        )
        pct = get_platform_fee_percent(trainer=trainer)
        self.assertEqual(str(pct), "0")


class DependentBookingFlowTests(TestCase):
    def _create_trainer_with_slot(self, *, slot_capacity=5):
        user = User.objects.create_user(
            username="trainer.dep@example.com",
            email="trainer.dep@example.com",
            password="pass12345",
        )
        trainer = Trainer.objects.create(
            user=user,
            business_name="Dependent Trainer",
            ath_mobile_handle="@dep",
            email_verified=True,
            active=True,
            session_price="50.00",
        )
        TrainerAvailability.objects.create(
            trainer=trainer,
            weekday=0,
            start_time=time(9, 0),
            end_time=time(10, 0),
            active=True,
        )
        slot = TimeSlot.objects.create(
            trainer=trainer,
            date=timezone.localdate() + timedelta(days=1),
            time=time(9, 0),
            capacity=slot_capacity,
            duration_minutes=60,
            active=True,
        )
        return trainer, slot

    def _create_logged_in_client_profile(self, *, email="client.dep@example.com", full_name="Client Dep"):
        user = User.objects.create_user(
            username=email,
            email=email,
            password="pass12345",
        )
        profile = ClientProfile.objects.create(
            user=user,
            full_name=full_name,
            active=True,
        )
        self.client.force_login(user)
        return user, profile

    def test_booking_with_self_and_dependent_creates_two_reservations(self):
        trainer, slot = self._create_trainer_with_slot()
        user, profile = self._create_logged_in_client_profile()
        dep = ClientDependent.objects.create(profile=profile, full_name="Carolina", relationship="Pareja", active=True)

        response = self.client.post(
            reverse("booking:checkout", kwargs={"slug": trainer.slug}) + "?week=current",
            {
                "name": profile.full_name,
                "email": user.email,
                "phone": "",
                "payment_method": Reservation.PAYMENT_ATH,
                "timeslot_ids": [str(slot.id)],
                f"attendees_{slot.id}": ["self", f"dep:{dep.id}"],
            },
        )

        self.assertEqual(response.status_code, 200)
        checkout = Checkout.objects.get(trainer=trainer)
        self.assertEqual(str(checkout.total_amount), "100.00")
        reservations = Reservation.objects.filter(checkout=checkout).order_by("attendee_type", "attendee_name")
        self.assertEqual(reservations.count(), 2)
        self.assertTrue(reservations.filter(attendee_type=Reservation.ATTENDEE_SELF, attendee_key="self").exists())
        self.assertTrue(reservations.filter(attendee_type=Reservation.ATTENDEE_DEPENDENT, dependent=dep).exists())

    def test_booking_only_for_dependent_creates_single_dependent_reservation(self):
        trainer, slot = self._create_trainer_with_slot()
        user, profile = self._create_logged_in_client_profile(email="client.dep2@example.com", full_name="Client Dep 2")
        dep = ClientDependent.objects.create(profile=profile, full_name="Hijo Uno", relationship="Hijo", active=True)

        response = self.client.post(
            reverse("booking:checkout", kwargs={"slug": trainer.slug}) + "?week=current",
            {
                "name": profile.full_name,
                "email": user.email,
                "phone": "",
                "payment_method": Reservation.PAYMENT_ATH,
                "timeslot_ids": [str(slot.id)],
                f"attendees_{slot.id}": [f"dep:{dep.id}"],
            },
        )

        self.assertEqual(response.status_code, 200)
        checkout = Checkout.objects.get(trainer=trainer)
        self.assertEqual(str(checkout.total_amount), "50.00")
        reservations = Reservation.objects.filter(checkout=checkout)
        self.assertEqual(reservations.count(), 1)
        reservation = reservations.first()
        self.assertEqual(reservation.attendee_type, Reservation.ATTENDEE_DEPENDENT)
        self.assertEqual(reservation.dependent_id, dep.id)
        self.assertEqual(reservation.attendee_name, dep.full_name)

    def test_capacity_validation_counts_all_requested_attendees(self):
        trainer, slot = self._create_trainer_with_slot(slot_capacity=2)
        user, profile = self._create_logged_in_client_profile(email="client.dep3@example.com", full_name="Client Dep 3")
        dep = ClientDependent.objects.create(profile=profile, full_name="Dependiente 3", active=True)

        # Existing reservation occupies 1 seat.
        other_client = Client.objects.create(trainer=trainer, name="Other", email="other@example.com")
        Reservation.objects.create(
            trainer=trainer,
            client=other_client,
            timeslot=slot,
            amount_due="50.00",
            payment_method=Reservation.PAYMENT_ATH,
            paid=False,
        )

        response = self.client.post(
            reverse("booking:checkout", kwargs={"slug": trainer.slug}) + "?week=current",
            {
                "name": profile.full_name,
                "email": user.email,
                "phone": "",
                "payment_method": Reservation.PAYMENT_ATH,
                "timeslot_ids": [str(slot.id)],
                f"attendees_{slot.id}": ["self", f"dep:{dep.id}"],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "se llenó")
        self.assertFalse(Checkout.objects.filter(client__email=user.email, trainer=trainer).exists())

    def test_duplicate_attendee_same_slot_is_blocked(self):
        trainer, slot = self._create_trainer_with_slot()
        user, profile = self._create_logged_in_client_profile(email="client.dep4@example.com", full_name="Client Dep 4")
        client = Client.objects.create(
            trainer=trainer,
            user=user,
            name=profile.full_name,
            email=user.email,
        )
        Reservation.objects.create(
            trainer=trainer,
            client=client,
            timeslot=slot,
            amount_due="50.00",
            payment_method=Reservation.PAYMENT_ATH,
            paid=False,
            attendee_type=Reservation.ATTENDEE_SELF,
            attendee_name=profile.full_name,
            attendee_key="self",
        )

        response = self.client.post(
            reverse("booking:checkout", kwargs={"slug": trainer.slug}) + "?week=current",
            {
                "name": profile.full_name,
                "email": user.email,
                "phone": "",
                "payment_method": Reservation.PAYMENT_ATH,
                "timeslot_ids": [str(slot.id)],
                f"attendees_{slot.id}": ["self"],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ya tiene reserva")
        self.assertEqual(Checkout.objects.filter(client=client, trainer=trainer).count(), 0)


class TwoFactorAuthTests(TestCase):
    def test_login_with_enabled_2fa_redirects_to_verification(self):
        user = User.objects.create_user(
            username="2fa-user@example.com",
            email="2fa-user@example.com",
            password="pass12345",
        )
        UserTwoFactorAuth.objects.create(
            user=user,
            is_enabled=True,
            totp_secret="JBSWY3DPEHPK3PXP",
            backup_codes=[],
        )

        response = self.client.post(
            reverse("login"),
            {"username": "2fa-user@example.com", "password": "pass12345"},
        )

        self.assertRedirects(response, reverse("booking:two_factor_verify"))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.client.session.get("two_fa_pending_user_id"), user.id)

    def test_totp_verification_completes_login_and_redirects(self):
        user = User.objects.create_user(
            username="2fa-client@example.com",
            email="2fa-client@example.com",
            password="pass12345",
        )
        ClientProfile.objects.create(user=user, full_name="2FA Client", active=True)
        two_fa = UserTwoFactorAuth.objects.create(
            user=user,
            is_enabled=True,
            totp_secret="JBSWY3DPEHPK3PXP",
            backup_codes=[],
        )

        self.client.post(
            reverse("login"),
            {
                "username": "2fa-client@example.com",
                "password": "pass12345",
                "next": reverse("booking:client_portal_dashboard"),
            },
        )
        code = _totp_code(two_fa.totp_secret, for_counter=int(pytime.time() // 30))
        response = self.client.post(
            reverse("booking:two_factor_verify"),
            {"totp_code": code},
        )

        self.assertRedirects(response, reverse("booking:client_portal_dashboard"))
        self.assertIn("_auth_user_id", self.client.session)
