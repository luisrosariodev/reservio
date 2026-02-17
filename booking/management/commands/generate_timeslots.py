from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta, datetime
from booking.models import Trainer, TrainerAvailability, TimeSlot


def _daterange(start_date, end_date):
    """Genera fechas desde start_date (incluida) hasta end_date (incluida)."""
    for n in range((end_date - start_date).days + 1):
        yield start_date + timedelta(days=n)


class Command(BaseCommand):
    help = "Genera TimeSlots a partir de TrainerAvailability para los próximos días"

    def handle(self, *args, **options):
        today = timezone.localdate()  # fecha en Puerto Rico según TIME_ZONE

        trainers = Trainer.objects.filter(active=True)

        total_created = 0

        for trainer in trainers:
            window_days = trainer.booking_window_days
            start_date = today
            end_date = today + timedelta(days=window_days)

            self.stdout.write(
                self.style.NOTICE(
                    f"Procesando trainer: {trainer.business_name} (window: {window_days} días)"
                )
            )

            availabilities = TrainerAvailability.objects.filter(
                trainer=trainer, active=True
            )

            if not availabilities.exists():
                self.stdout.write(
                    self.style.WARNING("  - No tiene disponibilidades configuradas.")
                )
                continue

            for single_date in _daterange(start_date, end_date):
                weekday = single_date.weekday()  # 0 = lunes, 6 = domingo

                # Filtrar las disponibilidades que aplican a ese día de la semana
                day_availabilities = availabilities.filter(weekday=weekday)

                for availability in day_availabilities:
                    # Generar los horarios dentro del bloque [start_time, end_time)
                    start_dt = datetime.combine(single_date, availability.start_time)
                    end_dt = datetime.combine(single_date, availability.end_time)

                    step = timedelta(minutes=availability.slot_duration_minutes)
                    current = start_dt

                    while current + step <= end_dt:
                        slot_time = current.time()

                        exists = TimeSlot.objects.filter(
                            trainer=trainer,
                            date=single_date,
                            time=slot_time,
                        ).exists()

                        if not exists:
                            TimeSlot.objects.create(
                                trainer=trainer,
                                date=single_date,
                                time=slot_time,
                                capacity=availability.slot_capacity,
                                active=True,
                            )
                            total_created += 1
                            self.stdout.write(
                                f"  - Creado slot: {single_date} {slot_time} (cap={availability.slot_capacity})"
                            )

                        current += step

        self.stdout.write(
            self.style.SUCCESS(f"Procesamiento completado. Slots creados: {total_created}")
        )