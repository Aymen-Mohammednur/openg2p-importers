import base64
import json
import logging
import mimetypes
from datetime import datetime

import pyjq
import requests

from odoo import _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class ODKClient:
    def __init__(
        self,
        env,
        _id,
        base_url,
        username,
        password,
        project_id,
        form_id,
        target_registry,
        json_formatter=".",
    ):
        self.id = _id
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.project_id = project_id
        self.form_id = form_id
        self.session = None
        self.env = env
        self.json_formatter = json_formatter
        self.target_registry = target_registry

    def login(self):
        login_url = f"{self.base_url}/v1/sessions"
        headers = {"Content-Type": "application/json"}
        data = json.dumps({"email": self.username, "password": self.password})
        try:
            response = requests.post(login_url, headers=headers, data=data, timeout=10)
            response.raise_for_status()
            if response.status_code == 200:
                self.session = response.json()["token"]
        except Exception as e:
            _logger.exception("Login failed: %s", e)
            raise ValidationError(f"Login failed: {e}") from e

    def test_connection(self):
        if not self.session:
            raise ValidationError(_("Session not created"))
        info_url = f"{self.base_url}/v1/users/current"
        headers = {"Authorization": f"Bearer {self.session}"}
        try:
            response = requests.get(info_url, headers=headers, timeout=10)
            response.raise_for_status()
            if response.status_code == 200:
                user = response.json()
                _logger.info(f'Connected to ODK Central as {user["displayName"]}')
                return True
        except Exception as e:
            _logger.exception("Connection test failed: %s", e)
            raise ValidationError(f"Connection test failed: {e}") from e

    # ruff: noqa: C901
    def import_delta_records(
        self,
        last_sync_timestamp=None,
        skip=0,
        top=100,
    ):
        url = f"{self.base_url}/v1/projects/{self.project_id}/forms/{self.form_id}.svc/Submissions"
        if last_sync_timestamp:
            startdate = last_sync_timestamp.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            params = {
                "$top": top,
                "$skip": skip,
                "$count": "true",
                "$expand": "*",
                "$filter": "__system/submissionDate ge " + startdate,
            }
        else:
            params = {"$top": top, "$skip": skip, "$count": "true", "$expand": "*"}

        headers = {"Authorization": f"Bearer {self.session}"}
        try:
            response = requests.get(url, headers=headers, params=params, timeout=10)
            response.raise_for_status()
        except Exception as e:
            _logger.exception("Failed to parse response: %s", e)
            raise ValidationError(f"Failed to parse response: {e}") from e
        data = response.json()
        for member in data["value"]:
            try:
                mapped_json = pyjq.compile(self.json_formatter).all(member)[0]
                if self.target_registry == "individual":
                    mapped_json.update({"is_registrant": True, "is_group": False})
                elif self.target_registry == "group":
                    mapped_json.update({"is_registrant": True, "is_group": True})

                # TODO: Handle many one2many based on requirements
                # phone one2many
                if "phone_number_ids" in mapped_json:
                    mapped_json["phone_number_ids"] = [
                        (
                            0,
                            0,
                            {
                                "phone_no": phone.get("phone_no", None),
                                "date_collected": phone.get("date_collected", None),
                                "disabled": phone.get("disabled", None),
                            },
                        )
                        for phone in mapped_json["phone_number_ids"]
                    ]

                # Membership one2many
                if "group_membership_ids" in mapped_json and self.target_registry == "group":
                    individual_ids = []
                    relationships_ids = []
                    for individual_mem in mapped_json.get("group_membership_ids"):
                        individual_data = self.get_individual_data(individual_mem)
                        individual = self.env["res.partner"].sudo().create(individual_data)
                        if individual:
                            kind = self.get_member_kind(individual_mem)
                            individual_data = {"individual": individual.id}

                            if kind:
                                individual_data["kind"] = [(4, kind.id)]

                            relationship = self.get_member_relationship(individual.id, individual_mem)

                            if relationship:
                                relationships_ids.append((0, 0, relationship))

                            individual_ids.append((0, 0, individual_data))

                    mapped_json["related_1_ids"] = relationships_ids
                    mapped_json["group_membership_ids"] = individual_ids

                # Reg_ids one2many
                if "reg_ids" in mapped_json:
                    mapped_json["reg_ids"] = [
                        (
                            0,
                            0,
                            {
                                "id_type": self.env["g2p.id.type"]
                                .search(
                                    [("name", "=", reg_id.get("id_type", None))],
                                )[0]
                                .id,
                                "value": reg_id.get("value", None),
                                "expiry_date": reg_id.get("expiry_date", None),
                            },
                        )
                        for reg_id in mapped_json["reg_ids"]
                    ]

                # media import
                meta = member.get("meta")
                if meta:
                    instance_id = meta.get("instanceID")
                    if instance_id:
                        exit_attachment = self.list_expected_attachments(
                            self.base_url, self.project_id, self.form_id, instance_id, self.session
                        )
                        if exit_attachment:
                            first_image_stored = False
                            for attachment in exit_attachment:
                                filename = attachment["name"]
                                get_attachment = self.download_attachment(
                                    self.base_url,
                                    self.project_id,
                                    self.form_id,
                                    instance_id,
                                    filename,
                                    self.session,
                                )
                                attachment_base64 = base64.b64encode(get_attachment).decode("utf-8")
                                image_verify = self.is_image(filename)
                                # verify the image mimetype and check image field available
                                # check image field are available if not map into ths supporting document
                                # store first image and other document store into the supporting document
                                if not first_image_stored and image_verify and "image_1920" in mapped_json:
                                    mapped_json.update({"image_1920": attachment_base64})
                                    first_image_stored = True
                                else:
                                    backend_id = (
                                        self.env.ref("storage_backend.default_storage_backend").id
                                        or self.env["storage.backend"].search([], limit=1).id
                                    )
                                    mapped_json["supporting_documents_ids"] = [
                                        (
                                            0,
                                            0,
                                            {
                                                "backend_id": backend_id,
                                                "name": attachment["name"],
                                                "data": attachment_base64,
                                            },
                                        )
                                    ]

                updated_mapped_json = self.get_addl_data(mapped_json)
                # update value into the res_partner table
                self.env["res.partner"].sudo().create(updated_mapped_json)
                data.update({"form_updated": True})
            except AttributeError as ex:
                data.update({"form_failed": True})
                _logger.error("Attribute Error", ex)
            except Exception as ex:
                data.update({"form_failed": True})
                _logger.error("An exception occurred", ex)

        return data

    def get_member_kind(self, record):
        kind = None
        return kind

    def get_member_relationship(self, source_id, record):
        # TODO: Add Member Relationship code
        relationship = None
        return relationship

    def get_gender(self, gender_val):
        if gender_val:
            gender = self.env["gender.type"].sudo().search([("code", "=", gender_val)], limit=1)
            if gender:
                return gender.code
            else:
                return None
        else:
            return None

    def get_dob(self, record):
        dob = record.get("birthdate", None)
        if dob:
            return dob

        age = record.get("age", None)
        if age:
            now = datetime.now()
            birth_year = now.year - age

            if birth_year < 0:
                _logger.warning("Future birthdate is not allowed.")
                return None
            return now.replace(year=birth_year).strftime("%Y-%m-%d")

    def get_individual_data(self, record):
        name = record.get("name", None)
        given_name = name.split(" ")[0]
        family_name = name.split(" ")[-1]
        dob = self.get_dob(record)
        addl_name = " ".join(name.split(" ")[1:-1])
        gender = self.get_gender(record.get("gender"))

        vals = {
            "name": name,
            "given_name": given_name,
            "family_name": family_name,
            "addl_name": addl_name,
            "is_registrant": True,
            "is_group": False,
            "birthdate": dob,
            "gender": gender,
        }

        return vals

    def get_addl_data(self, mapped_json):
        # Override this method to add more data

        return mapped_json

    # Determine if the file is an image based on its MIME type
    def is_image(self, filename):
        mimetype, _ = mimetypes.guess_type(filename)
        return mimetype and mimetype.startswith("image")

    # fetch list of attachment
    def list_expected_attachments(self, base_url, project_id, form_id, instance_id, session_token):
        url = f"{base_url}/v1/projects/{project_id}/forms/{form_id}/submissions/{instance_id}/attachments"
        headers = {"Authorization": f"Bearer {session_token}"}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()

    # download the attachement
    def download_attachment(self, base_url, project_id, form_id, instance_id, filename, session_token):
        url = (
            f"{base_url}/v1/projects/{project_id}/forms/{form_id}/"
            f"submissions/{instance_id}/attachments/{filename}"
        )
        headers = {"Authorization": f"Bearer {session_token}"}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.content
