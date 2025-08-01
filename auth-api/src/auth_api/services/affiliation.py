# Copyright © 2023 Province of British Columbia
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Service for managing Affiliation data."""
import datetime
import re
from dataclasses import asdict
from typing import Dict, List, Tuple

from flask import current_app
from requests.exceptions import HTTPError
from sbc_common_components.utils.enums import QueueMessageTypes
from sqlalchemy.orm import contains_eager, subqueryload

from auth_api.exceptions import BusinessException, ServiceUnavailableException
from auth_api.exceptions.errors import Error
from auth_api.models import db
from auth_api.models.affiliation import Affiliation as AffiliationModel
from auth_api.models.affiliation_invitation import AffiliationInvitation as AffiliationInvitationModel
from auth_api.models.contact_link import ContactLink
from auth_api.models.dataclass import Activity
from auth_api.models.dataclass import Affiliation as AffiliationData
from auth_api.models.dataclass import AffiliationBase, AffiliationSearchDetails, DeleteAffiliationRequest
from auth_api.models.entity import Entity
from auth_api.models.membership import Membership as MembershipModel
from auth_api.schemas import AffiliationSchema
from auth_api.services.entity import Entity as EntityService
from auth_api.services.org import Org as OrgService
from auth_api.services.user import User as UserService
from auth_api.utils.enums import ActivityAction, CorpType, NRActionCodes, NRNameStatus, NRStatus
from auth_api.utils.passcode import validate_passcode
from auth_api.utils.roles import AFFILIATION_ALLOWED_ROLES, ALL_ALLOWED_ROLES, CLIENT_AUTH_ROLES, STAFF, Role
from auth_api.utils.user_context import UserContext, user_context

from ..utils.auth_event_publisher import publish_affiliation_event
from .activity_log_publisher import ActivityLogPublisher
from .rest_service import RestService


class Affiliation:
    """Manages all aspect of Affiliation data.

    This manages updating, retrieving, and creating Affiliation data via the Affiliation model.
    """

    def __init__(self, model):
        """Return an Affiliation Service."""
        self._model = model

    @property
    def identifier(self):
        """Return the unique identifier for this model."""
        return self._model.id

    @property
    def entity(self):
        """Return the entity for this affiliation as a service."""
        return EntityService(self._model.entity)

    def as_dict(self):
        """Return the affiliation as a python dictionary.

        None fields are not included in the dictionary.
        """
        affiliation_schema = AffiliationSchema()
        obj = affiliation_schema.dump(self._model, many=False)
        return obj

    @staticmethod
    def find_visible_affiliations_by_org_id(org_id):
        """Given an org_id, this will return the entities affiliated with it."""
        current_app.logger.debug(f"<find_visible_affiliations_by_org_id for org_id {org_id}")
        org = OrgService.find_by_org_id(org_id, allowed_roles=AFFILIATION_ALLOWED_ROLES)
        if org is None:
            raise BusinessException(Error.DATA_NOT_FOUND, None)

        data = Affiliation.find_affiliations_by_org_id(org_id)

        # 3806 : Filter out the NR affiliation if there is IA affiliation for the same NR.
        nr_number_name_dict = {
            d["business_identifier"]: d["name"] for d in data if d["corp_type"]["code"] == CorpType.NR.value
        }
        nr_numbers = nr_number_name_dict.keys()
        filtered_affiliations = Affiliation.filter_affiliations(data, nr_numbers, nr_number_name_dict)
        current_app.logger.debug(">find_visible_affiliations_by_org_id")
        return filtered_affiliations

    @staticmethod
    def filter_affiliations(data, nr_numbers, nr_number_name_dict: dict):
        """Filter affiliations."""
        temp_types = {CorpType.TMP.value, CorpType.ATMP.value, CorpType.CTMP.value, CorpType.RTMP.value}
        tmp_business_list = {d["name"] for d in data if d["corp_type"]["code"] in temp_types}
        filtered_affiliations = []

        for entity in data:
            code = entity["corp_type"]["code"]
            name = entity["name"]
            identifier = entity["business_identifier"]

            if code == CorpType.NR.value and identifier in tmp_business_list:
                continue

            if code in temp_types:
                # Only include if named company IA or numbered company
                # Skip temp unless it's a numbered company or matches NR
                if name not in nr_numbers and name != identifier:
                    continue
                if name in nr_numbers:
                    entity.update({"nr_number": name, "name": nr_number_name_dict[name]})

            filtered_affiliations.append(entity)

        return filtered_affiliations

    @staticmethod
    def find_affiliations_by_org_id(org_id):
        """Return business affiliations for the org."""
        # Accomplished in service instead of model (easier to avoid circular reference issues).
        entities = (
            db.session.query(Entity)
            .join(AffiliationModel)
            .options(
                contains_eager(Entity.affiliations),
                subqueryload(Entity.contacts).subqueryload(ContactLink.contact),
                subqueryload(Entity.created_by),
                subqueryload(Entity.modified_by),
            )
            .filter(
                AffiliationModel.org_id == int(org_id or -1),
                Entity.affiliations.any(AffiliationModel.org_id == int(org_id or -1)),
            )
        )
        entities = entities.order_by(AffiliationModel.created.desc()).all()
        return [EntityService(entity).as_dict() for entity in entities]

    @staticmethod
    def find_affiliation(org_id, business_identifier):
        """Return business affiliation by the org id and business identifier."""
        affiliation = AffiliationModel.find_affiliation_by_org_id_and_business_identifier(org_id, business_identifier)
        if affiliation is None:
            raise BusinessException(Error.DATA_NOT_FOUND, None)
        return Affiliation(affiliation).as_dict()

    @staticmethod
    def create_affiliation(
        org_id,
        business_identifier,
        pass_code=None,
        certified_by_name=None,
        skip_membership_check=False,
    ):
        """Create an Affiliation."""
        # Validate if org_id is valid by calling Org Service.
        current_app.logger.info(f"<create_affiliation org_id:{org_id} business_identifier:{business_identifier}")
        if skip_membership_check is False:
            org = OrgService.find_by_org_id(org_id, allowed_roles=ALL_ALLOWED_ROLES)
            if org is None:
                raise BusinessException(Error.DATA_NOT_FOUND, None)

        entity = EntityService.find_by_business_identifier(business_identifier, skip_auth=True)
        if entity is None:
            raise BusinessException(Error.DATA_NOT_FOUND, None)
        current_app.logger.debug("<create_affiliation entity found")
        entity_id = entity.identifier
        entity_type = entity.corp_type

        if not Affiliation.is_authorized(entity, pass_code):
            current_app.logger.debug("<create_affiliation not authorized")
            raise BusinessException(Error.INVALID_USER_CREDENTIALS, None)

        current_app.logger.debug("<create_affiliation find affiliation")
        # Ensure this affiliation does not already exist
        affiliation = AffiliationModel.find_affiliation_by_org_and_entity_ids(org_id, entity_id)
        if affiliation is not None:
            raise BusinessException(Error.DATA_ALREADY_EXISTS, None)

        affiliation = AffiliationModel(org_id=org_id, entity_id=entity_id, certified_by_name=certified_by_name)
        affiliation.save()

        if entity_type not in ["SP", "GP"]:
            entity.set_pass_code_claimed(True)
        if entity_type not in [CorpType.RTMP.value, CorpType.TMP.value, CorpType.ATMP.value, CorpType.CTMP.value]:
            name = entity.name if len(entity.name) > 0 else entity.business_identifier
            ActivityLogPublisher.publish_activity(
                Activity(org_id, ActivityAction.CREATE_AFFILIATION.value, name=name, id=entity.business_identifier)
            )

        publish_affiliation_event(QueueMessageTypes.BUSINESS_AFFILIATED.value, org_id, entity.business_identifier)
        return Affiliation(affiliation)

    @staticmethod
    def is_authorized(entity: Entity, pass_code: str) -> bool:
        """Return True if user is authorized to create an affiliation."""
        if Affiliation.has_role_to_skip_auth():
            return True
        if entity.corp_type in ["SP", "GP"]:
            if not pass_code:
                return False
            token = RestService.get_service_account_token(
                config_id="ENTITY_SVC_CLIENT_ID", config_secret="ENTITY_SVC_CLIENT_SECRET"
            )
            return Affiliation._validate_firms_party(token, entity.business_identifier, pass_code)
        if pass_code:
            return validate_passcode(pass_code, entity.pass_code)
        if entity.pass_code:
            return False
        return True

    @staticmethod
    def create_new_business_affiliation(affiliation_data: AffiliationData):  # pylint: disable=too-many-locals
        """Initiate a new incorporation."""
        org_id = affiliation_data.org_id
        business_identifier = affiliation_data.business_identifier
        certified_by_name = affiliation_data.certified_by_name

        current_app.logger.info(f"<create_affiliation org_id:{org_id} business_identifier:{business_identifier}")

        entity, nr_json = Affiliation.validate_new_business_affiliation(affiliation_data)
        status = nr_json.get("state")
        # Create an entity with the Name from NR if entity doesn't exist
        if not entity:
            # Filter the names from NR response and get the name which has status APPROVED as the name.
            # Filter the names from NR response and get the name which has status CONDITION as the name.
            nr_name_state = (
                NRNameStatus.APPROVED.value if status == NRStatus.APPROVED.value else NRNameStatus.CONDITION.value
            )
            name = next(
                (name.get("name") for name in nr_json.get("names") if name.get("state", None) == nr_name_state), None
            )

            entity = EntityService.save_entity(
                {
                    "businessIdentifier": business_identifier,
                    "name": name or business_identifier,
                    "corpTypeCode": CorpType.NR.value,
                    "passCodeClaimed": True,
                }
            )

        # Affiliation may already already exist.
        if not (
            affiliation_model := AffiliationModel.find_affiliation_by_org_and_entity_ids(org_id, entity.identifier)
        ):
            # Create an affiliation with org
            affiliation_model = AffiliationModel(
                org_id=org_id, entity_id=entity.identifier, certified_by_name=certified_by_name
            )

            if entity.corp_type not in [
                CorpType.RTMP.value,
                CorpType.TMP.value,
                CorpType.ATMP.value,
                CorpType.CTMP.value,
            ]:
                ActivityLogPublisher.publish_activity(
                    Activity(
                        org_id, ActivityAction.CREATE_AFFILIATION.value, name=entity.name, id=entity.business_identifier
                    )
                )
        affiliation_model.certified_by_name = certified_by_name
        affiliation_model.save()
        entity.set_pass_code_claimed(True)

        return Affiliation(affiliation_model)

    @staticmethod
    def validate_new_business_affiliation(affiliation_data: AffiliationData):
        """Validate the new business affiliation."""
        org_id = affiliation_data.org_id
        business_identifier = affiliation_data.business_identifier
        email = affiliation_data.email
        phone = affiliation_data.phone

        user_is_staff = Affiliation.has_role_to_skip_auth()
        if not user_is_staff and not (email or phone):
            raise BusinessException(Error.NR_INVALID_CONTACT, None)

        Affiliation._validate_org_exists(org_id)
        entity = EntityService.find_by_business_identifier(business_identifier, skip_auth=True)
        nr_json = Affiliation._get_and_validate_nr_details(business_identifier)

        if nr_json.get("state") == NRStatus.DRAFT.value:
            Affiliation._validate_nr_payment(business_identifier)

        if not user_is_staff and not Affiliation._contacts_match(phone, email, nr_json):
            raise BusinessException(Error.NR_INVALID_CONTACT, None)

        return entity, nr_json

    @staticmethod
    def _validate_org_exists(org_id):
        """Validate if org_id is valid by calling Org Service."""
        org = OrgService.find_by_org_id(org_id, allowed_roles=(*CLIENT_AUTH_ROLES, STAFF))
        if org is None:
            raise BusinessException(Error.DATA_NOT_FOUND, None)

    @staticmethod
    def _get_and_validate_nr_details(business_identifier):
        """Get and validate NR details."""
        nr_json = Affiliation._get_nr_details(business_identifier)
        if not nr_json:
            raise BusinessException(Error.NR_NOT_FOUND, None)

        status = nr_json.get("state")
        valid_statuses = {
            NRStatus.APPROVED.value,
            NRStatus.CONDITIONAL.value,
            NRStatus.DRAFT.value,
            NRStatus.INPROGRESS.value,
        }
        if status not in valid_statuses:
            raise BusinessException(Error.NR_INVALID_STATUS, None)

        if not nr_json.get("applicants"):
            raise BusinessException(Error.NR_INVALID_APPLICANTS, None)

        return nr_json

    @staticmethod
    def _validate_nr_payment(business_identifier):
        """Validate NR payment status."""
        invoices = Affiliation.get_nr_payment_details(business_identifier)
        if not (invoices and invoices.get("invoices") and invoices["invoices"][0].get("statusCode") == "COMPLETED"):
            raise BusinessException(Error.NR_NOT_PAID, None)

    @staticmethod
    def _contacts_match(phone, email, nr_json):
        """Check if the provided phone and email match the NR details."""
        applicants = nr_json.get("applicants", {})
        nr_phone = applicants.get("phoneNumber") or ""
        nr_email = applicants.get("emailAddress") or ""

        phone_match = not phone or re.sub(r"\D", "", phone) == re.sub(r"\D", "", nr_phone)
        email_match = not email or email.casefold() == nr_email.casefold()

        return phone_match and email_match

    @staticmethod
    def get_nr_payment_details(business_identifier):
        """Get the NR payment details."""
        pay_api_url = current_app.config.get("PAY_API_URL")
        invoices = RestService.get(
            f"{pay_api_url}/payment-requests?businessIdentifier={business_identifier}",
            token=RestService.get_service_account_token(),
        ).json()
        return invoices

    @user_context
    @staticmethod
    def delete_affiliation(da: DeleteAffiliationRequest, **kwargs):
        """Delete the affiliation for the provided org id and business id."""
        user_from_context: UserContext = kwargs["user_context"]
        current_app.logger.info(f"<delete_affiliation org_id:{da.org_id} business_identifier:{da.business_identifier}")
        org = OrgService.find_by_org_id(
            da.org_id, allowed_roles=(*CLIENT_AUTH_ROLES, STAFF, Role.EXTERNAL_STAFF_READONLY.value)
        )
        if org is None:
            raise BusinessException(Error.DATA_NOT_FOUND, None)

        entity = EntityService.find_by_business_identifier(
            da.business_identifier,
            skip_auth=user_from_context.is_staff() or user_from_context.is_external_staff(),
            allowed_roles=(CLIENT_AUTH_ROLES),
        )
        if entity is None:
            raise BusinessException(Error.DATA_NOT_FOUND, None)

        entity_id = entity.identifier

        affiliation = AffiliationModel.find_affiliation_by_org_and_entity_ids(org_id=da.org_id, entity_id=entity_id)
        if affiliation is None:
            raise BusinessException(Error.DATA_NOT_FOUND, None)

        # Could possibly be a single row.
        for affiliation_invitation in AffiliationInvitationModel.find_invitations_by_affiliation(affiliation.id):
            affiliation_invitation.delete()

        if da.reset_passcode:
            entity.reset_passcode(entity.business_identifier, da.email_addresses)
        affiliation.delete()
        entity.set_pass_code_claimed(False)

        if entity.corp_type in [CorpType.RTMP.value, CorpType.TMP.value, CorpType.ATMP.value, CorpType.CTMP.value]:
            return

        # When registering a business (also RTMP and TMP in between):
        # 1. affiliate a NR
        # 2. unaffiliate a NR draft
        # 3. affiliate a business (with NR in identifier)
        # 4. unaffilliate a business (with NR in identifier)
        # 5. affilliate a business (with FM or BC in identifier)
        # Users can also intentionally delete a draft. We want to log this action.
        name_request = (
            entity.status in [NRStatus.DRAFT.value, NRStatus.CONSUMED.value] and entity.corp_type == CorpType.NR.value
        ) or "NR " in entity.business_identifier
        publish = da.log_delete_draft or not name_request
        if publish:
            name = entity.name if len(entity.name) > 0 else entity.business_identifier
            ActivityLogPublisher.publish_activity(
                Activity(da.org_id, ActivityAction.REMOVE_AFFILIATION.value, name=name, id=entity.business_identifier)
            )

        publish_affiliation_event(QueueMessageTypes.BUSINESS_UNAFFILIATED.value, da.org_id, entity.business_identifier)

    @staticmethod
    @user_context
    def fix_stale_affiliations(org_id: int, entity_details: Dict, **kwargs):
        """Corrects affiliations to point at the latest entity."""
        # Example staff/client scenario:
        # 1. client creates an NR (that gets affiliated) - realizes they need help to create a business
        # 2. staff takes NR, creates a business
        # 3. filer updates the business for staff (which creates a new entity)
        # 4. fix_stale_affiliations is called, and fixes the client's affiliation to point at this new entity
        user_from_context: UserContext = kwargs["user_context"]
        if not user_from_context.is_system():
            return
        nr_number: str = entity_details.get("nrNumber")
        bootstrap_identifier: str = entity_details.get("bootstrapIdentifier")
        identifier: str = entity_details.get("identifier")
        current_app.logger.debug(f"<fix_stale_affiliations - {nr_number} {bootstrap_identifier} {identifier}")
        from_entity: Entity = EntityService.find_by_business_identifier(nr_number, skip_auth=True)
        # Find entity with nr_number (stale, because this is now a business)
        if (
            from_entity
            and from_entity.corp_type == "NR"
            and (to_entity := EntityService.find_by_business_identifier(identifier, skip_auth=True))
        ):
            affiliations = AffiliationModel.find_affiliations_by_entity_id(from_entity.identifier)
            for affiliation in affiliations:
                # These are already handled by the filer.
                if affiliation.org_id == org_id:
                    continue
                current_app.logger.debug(
                    f"Moving affiliation {affiliation.id} from {from_entity.identifier} to {to_entity.identifier}"
                )
                affiliation.entity_id = to_entity.identifier
                affiliation.save()

        current_app.logger.debug(">fix_stale_affiliations")

    @staticmethod
    def _affiliation_details_url(identifier: str) -> str:
        """Determine url to call for affiliation details."""
        # only have LEAR and NAMEX affiliations
        if identifier.startswith("NR"):
            return current_app.config.get("NAMEX_AFFILIATION_DETAILS_URL")
        return current_app.config.get("LEAR_AFFILIATION_DETAILS_URL")

    @staticmethod
    def affiliation_to_affiliation_base(affiliations: List[AffiliationModel]) -> List[AffiliationBase]:
        """Convert affiliations to a common data class."""
        return [
            AffiliationBase(identifier=affiliation.entity.business_identifier, created=affiliation.created)
            for affiliation in affiliations
        ]

    @staticmethod
    async def get_affiliation_details(
        affiliation_bases: List[AffiliationBase],
        search_details: AffiliationSearchDetails,
        org_id,
        remove_stale_drafts,
    ) -> Tuple[List, bool]:
        """Return affiliation details by calling the source api."""
        url_identifiers = {}  # i.e. turns into { url: [identifiers...] }
        # Our pagination is already handled at the auth level when not doing a search.
        if not any([search_details.status, search_details.name, search_details.type, search_details.identifier]):
            search_details.page = 1
        search_dict = asdict(search_details)
        for affiliation_base in affiliation_bases:
            url = Affiliation._affiliation_details_url(affiliation_base.identifier)
            url_identifiers.setdefault(url, []).append(affiliation_base.identifier)
        call_info = [
            {
                "url": url,
                "payload": {
                    "identifiers": identifiers,
                    **search_dict,
                },
            }
            for url, identifiers in url_identifiers.items()
        ]

        token = RestService.get_service_account_token(
            config_id="ENTITY_SVC_CLIENT_ID", config_secret="ENTITY_SVC_CLIENT_SECRET"
        )
        try:
            responses = await RestService.call_posts_in_parallel(call_info, token, org_id)
            has_more_apis = any(r.get("hasMore", False) for r in responses if isinstance(r, dict))
            combined = Affiliation._combine_affiliation_details(responses, remove_stale_drafts)
            combined = Affiliation._sort_affiliations_by_created(combined, affiliation_bases)

            Affiliation._handle_affiliation_debug(affiliation_bases, combined)
            return combined, has_more_apis
        except ServiceUnavailableException as err:
            current_app.logger.debug(err)
            current_app.logger.debug("Failed to get affiliations details:  %s", affiliation_bases)
            raise ServiceUnavailableException("Failed to get affiliation details") from err

    @staticmethod
    def _sort_affiliations_by_created(combined: list, affiliation_bases: list) -> list:
        """Sort affiliations by created date."""
        ordered = {
            affiliation.identifier: affiliation.created
            for affiliation in sorted(affiliation_bases, key=lambda x: x.created, reverse=True)
        }

        def sort_key(item):
            return ordered.get(
                item.get("identifier", item.get("nameRequest", {}).get("nrNum", "")), datetime.datetime.min
            )

        combined.sort(key=sort_key, reverse=True)
        return combined

    @staticmethod
    def _handle_affiliation_debug(affiliation_bases, combined):
        """Enable affiliation debug."""
        if current_app.config.get("AFFILIATION_DEBUG") is False:
            return
        base_identifiers = {base.identifier for base in affiliation_bases}
        combined_identifiers = set()
        for item in combined:
            identifier = item.get("identifier") or item.get("nameRequest", {}).get("nrNum")
            if identifier:
                combined_identifiers.add(identifier)
        missing_identifiers = base_identifiers - combined_identifiers
        if missing_identifiers:
            current_app.logger.warning(f"Identifiers missing from combined results: {missing_identifiers}")

    @staticmethod
    def _extract_name_requests(data: dict | list) -> dict:
        """Updates Name Requests from the affiliation details response."""
        name_requests_key = "requests"
        normalized = {"hasMore": False, "requests": []}
        if isinstance(data, list):
            normalized["requests"] = data
            normalized["hasMore"] = False
            return normalized
        if isinstance(data, dict):
            nr_list = data.get(name_requests_key)
            if isinstance(nr_list, list):
                normalized["requests"] = nr_list
                normalized["hasMore"] = data.get("hasMore", False)

        return normalized

    @staticmethod
    def _group_details(details):
        """Group details from the affiliation details response."""
        name_requests = {}
        businesses = []
        drafts = []
        businesses_key = "businessEntities"
        drafts_key = "draftEntities"
        name_requests_key = "requests"
        for data in details:
            nr_data = Affiliation._extract_name_requests(data)
            for name_request in nr_data[name_requests_key]:
                if "nrNum" in name_request:
                    name_requests[name_request["nrNum"]] = {
                        "legalType": CorpType.NR.value,
                        "nameRequest": name_request,
                    }
            if businesses_key in data:
                businesses.extend(data.get(businesses_key))
            if drafts_key in data:
                drafts.extend(data.get(drafts_key))
        return name_requests, businesses, drafts

    @staticmethod
    def _update_draft_type_for_amalgamation_nr(business):
        # If the business is a draft and the NR is an amalgamation, set the draftType to ATMP.
        if (
            business.get("draftType", None)
            and business["nameRequest"]["request_action_cd"] == NRActionCodes.AMALGAMATE.value
        ):
            business["draftType"] = CorpType.ATMP.value
        return business

    @staticmethod
    def _process_nr_for_business(business, name_requests, drafts):
        """Process NR for a business entity."""
        nr_num = business["nrNumber"]
        if nr_num in name_requests:
            business["nameRequest"] = name_requests[nr_num]["nameRequest"]
            business = Affiliation._update_draft_type_for_amalgamation_nr(business)
            if business["nameRequest"]["stateCd"] == NRStatus.CONSUMED.value:
                drafts.remove(business)
            del name_requests[nr_num]
            return True
        return False

    @staticmethod
    def _combine_nrs(name_requests, businesses, drafts, remove_stale_drafts=True):
        """Combine NRs with the business and draft entities."""
        for business in drafts + businesses:
            if "nrNumber" in business and business["nrNumber"]:
                processed = Affiliation._process_nr_for_business(business, name_requests, drafts)
                if not processed and remove_stale_drafts and business in drafts:
                    drafts.remove(business)
        return list(name_requests.values()) + drafts + businesses

    @staticmethod
    def _combine_affiliation_details(details, remove_stale_drafts=True):
        """Parse affiliation details responses and combine draft entities with NRs if applicable."""
        name_requests, businesses, drafts = Affiliation._group_details(details)
        return Affiliation._combine_nrs(name_requests, businesses, drafts, remove_stale_drafts)

    @staticmethod
    def _get_nr_details(nr_number: str):
        """Return NR details by calling legal-api."""
        nr_api_url = current_app.config.get("NAMEX_API_URL")
        get_nr_url = f"{nr_api_url}/requests/{nr_number}"
        try:
            token = RestService.get_service_account_token(
                config_id="ENTITY_SVC_CLIENT_ID", config_secret="ENTITY_SVC_CLIENT_SECRET"
            )
            get_nr_response = RestService.get(get_nr_url, token=token, skip_404_logging=True)
        except (HTTPError, ServiceUnavailableException) as e:
            current_app.logger.info(e)
            raise BusinessException(Error.DATA_NOT_FOUND, None) from e
        return get_nr_response.json()

    @staticmethod
    def _validate_firms_party(token, business_identifier, party_name_str: str):
        """Validate if the party name is in the firms party list."""
        legal_api_url = current_app.config.get("LEGAL_API_URL") + current_app.config.get("LEGAL_API_VERSION_2")

        parties_url = f"{legal_api_url}/businesses/{business_identifier}/parties"
        try:
            lear_response = RestService.get(parties_url, token=token, skip_404_logging=True)
        except (HTTPError, ServiceUnavailableException) as e:
            current_app.logger.info(e)
            raise BusinessException(Error.DATA_NOT_FOUND, None) from e
        parties_json = lear_response.json()
        for party in parties_json["parties"]:
            officer = party.get("officer")
            if officer.get("partyType") == "organization":
                party_name = officer.get("organizationName")
            else:
                party_name = officer.get("lastName") + ", " + officer.get("firstName")
                if officer.get("middleInitial"):
                    party_name = party_name + " " + officer.get("middleInitial")

            # remove duplicate spaces
            party_name_str = " ".join(party_name_str.split())
            party_name = " ".join(party_name.split())

            if party_name_str.upper() == party_name.upper():
                return True
        return False

    @staticmethod
    @user_context
    def has_role_to_skip_auth(**kwargs):
        """Return True if user is staff or sbc staff."""
        user_from_context: UserContext = kwargs["user_context"]
        current_user: UserService = UserService.find_by_jwt_token(silent_mode=True)
        if (
            user_from_context.has_role(Role.SKIP_AFFILIATION_AUTH.value)
            or user_from_context.is_staff()
            or (current_user and MembershipModel.check_if_sbc_staff(current_user.identifier))
        ):
            return True
        return False
