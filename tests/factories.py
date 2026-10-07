"""Test data factories using factory-boy.

Provides factory classes for creating test data consistently.
"""

from __future__ import annotations

import uuid

import factory
from faker import Faker

from app.auth.models import Permission, Role, User
from app.auth.permissions import PermissionEnum, RoleEnum
from app.common.ids import uuid7
from app.customers.models import Customer
from app.vehicles.models import MileageSource, Vehicle, VehicleMileageRecord, VehicleStatus

fake = Faker()


class RoleFactory(factory.Factory):
    """Factory for creating Role instances."""

    class Meta:
        model = Role

    name = factory.Iterator(lambda: [r.value for r in RoleEnum])
    description = factory.LazyAttribute(lambda o: f"{o.name} role")


class PermissionFactory(factory.Factory):
    """Factory for creating Permission instances."""

    class Meta:
        model = Permission

    name = factory.Iterator(lambda: [p.value for p in PermissionEnum])
    description = factory.LazyAttribute(lambda o: f"Permission: {o.name}")


class UserFactory(factory.Factory):
    """Factory for creating User instances."""

    class Meta:
        model = User

    id = factory.LazyFunction(uuid7)
    email = factory.LazyAttribute(lambda o: f"{fake.user_name()}_{uuid.uuid4().hex[:8]}@test.com")
    password_hash = factory.LazyAttribute(lambda o: "$2b$12$fakehashedpasswordfortesting")
    first_name = factory.LazyAttribute(lambda o: fake.first_name())
    last_name = factory.LazyAttribute(lambda o: fake.last_name())
    phone = factory.LazyAttribute(lambda o: fake.phone_number())
    is_active = True
    is_staff = False


class CustomerFactory(factory.Factory):
    class Meta:
        model = Customer

    id = factory.LazyFunction(uuid7)
    first_name = factory.LazyAttribute(lambda o: fake.first_name())
    last_name = factory.LazyAttribute(lambda o: fake.last_name())
    company_name = factory.Faker("company")
    email = factory.LazyAttribute(lambda o: f"{fake.user_name()}_{uuid.uuid4().hex[:8]}@test.com")
    phone = factory.LazyAttribute(lambda o: fake.phone_number())
    preferred_contact = "EMAIL"
    customer_status = "ACTIVE"
    notes = factory.Faker("text", max_nb_chars=200)


class VehicleFactory(factory.Factory):
    class Meta:
        model = Vehicle

    id = factory.LazyFunction(uuid7)
    vin = factory.LazyAttribute(lambda o: uuid.uuid4().hex[:17].upper())
    license_plate = factory.LazyAttribute(lambda o: f"{fake.bothify(text='???-####')}")
    make = factory.Iterator(["Toyota", "Honda", "Ford", "Chevrolet", "Nissan"])
    model = factory.LazyAttribute(lambda o: fake.pystr(min_chars=3, max_chars=15))
    year = factory.LazyAttribute(lambda o: fake.random_int(min=2000, max=2025))
    color = factory.Faker("color_name")
    mileage = factory.LazyAttribute(lambda o: fake.random_int(min=0, max=100000))
    fuel_type = factory.Iterator(["GASOLINE", "DIESEL", "ELECTRIC", "HYBRID"])
    status = VehicleStatus.ACTIVE.value


class MileageRecordFactory(factory.Factory):
    class Meta:
        model = VehicleMileageRecord

    id = factory.LazyFunction(uuid7)
    mileage = factory.LazyAttribute(lambda o: fake.random_int(min=1000, max=200000))
    source = MileageSource.MANUAL.value
    notes = factory.Faker("text", max_nb_chars=100)
