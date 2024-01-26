from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from config import settings
from datetime import datetime
from app.validations import ValidationException
from app.controllers import agent, status_list, askar
from app.models.web_requests import (
    IssueCredentialSchema,
    UpdateCredentialStatusSchema,
    VerifyCredentialSchema,
    CredentialVerificationResponse,
)
from app.auth.bearer import JWTBearer
from app.auth.handler import is_authorized
from app.utils import format_label
import uuid

router = APIRouter()


@router.get(
    "/organization/{label}/credentials/{credential_id}",
    tags=["Credentials"],
    dependencies=[Depends(JWTBearer())],
    summary="Get a verifiable credential by id. Required to make revocable credentials.",
)
async def get_credential(label: str, credential_id: str, request: Request):
    label = is_authorized(label, request)
    try:
        data_key = f"{label}:credentials:{credential_id}"
        credential = await askar.fetch_data(settings.ASKAR_KEY, data_key)
        return credential
    except:
        raise ValidationException(
            status_code=404,
            content={"message": f"credential {credential_id} not found"},
        )


@router.post(
    "/organization/{label}/credentials/issue",
    tags=["Credentials"],
    dependencies=[Depends(JWTBearer())],
    summary="Issue a credential",
)
async def issue_credential(
    label: str, request: Request, request_body: IssueCredentialSchema
):
    label = is_authorized(label, request)

    request_body = request_body.dict(exclude_none=True)
    credential = request_body["credential"]
    credential["@context"] = credential.pop("context")
    options = request_body["options"]
    # Generate a credential id if none is provided
    if "id" not in credential:
        credential["id"] = f"urn:uuid:{str(uuid.uuid4())}"

    # Fill status information
    if "credentialStatus" in options:
        if options["credentialStatus"]["type"] == "StatusList2021Entry":
            credential["@context"].append("https://w3id.org/vc/status-list/2021/v1")
            credential["credentialStatus"] = await status_list.create_entry(
                label, "StatusList2021"
            )
        elif options["credentialStatus"]["type"] == "RevocationList2020Status":
            credential["@context"].append("https://w3id.org/vc-revocation-list-2020/v1")
            credential["credentialStatus"] = await status_list.create_entry(
                label, "RevocationList2020"
            )
    options.pop("credentialStatus")
    
    issuer_did = (
        credential["issuer"]
        if isinstance(credential["issuer"], str)
        else credential["issuer"]["id"]
    )
    did = f"{settings.DID_WEB_BASE}:organization:{label}"
    if did != issuer_did:
        raise ValidationException(
            status_code=400, content={"message": "Invalid issuer"}
        )
    # Limited to 1 verification_key per did and we use #verkey as id
    options["verificationMethod"] = f"{did}#verkey"

    # Backwards compatibility with old json-ld routes in traction,
    # doesn't support created option and requires proofPurpose
    if "created" in options:
        options.pop("created")
    options["proofPurpose"] = "assertionMethod"
    verkey = agent.get_verkey(did)
    print(credential)
    print(options)
    vc = agent.sign_json_ld(credential, options, verkey)

    # New vc-api routes
    # vc = agent.issue_credential(credential, options, token)

    data_key = f'{label}:credentials:{credential["id"]}'.lower()
    await askar.store_data(settings.ASKAR_KEY, data_key, credential)

    return JSONResponse(status_code=201, content={"verifiableCredential": vc})


@router.post(
    "/organization/{label}/credentials/verify",
    tags=["Credentials"],
    dependencies=[Depends(JWTBearer())],
    summary="Verify a credential",
)
async def verify_credential(
    label: str, request: Request, request_body: VerifyCredentialSchema
):
    label = is_authorized(label, request)

    request_body = request_body.dict(exclude_none=True)
    vc = request_body["verifiableCredential"]
    vc["@context"] = vc.pop("context")
    verification = CredentialVerificationResponse()
    verification = verification.dict()

    # Check credential status
    if "credentialStatus" in vc:
        # vc['credentialStatus']['purpose']
        status_type = vc['credentialStatus']['type']
        verification["checks"] += ["status"]
        status = status_list.get_credential_status(vc, status_type)
        if status:
            verification["errors"] += ["revoked"]
            verification["verifications"] = [{"title": "Revocation", "status": "bad"}]

    # Check expiration date
    if "expirationDate" in vc:
        verification["checks"].append("expiry")
        expiration_time = datetime.fromisoformat(vc["expirationDate"])
        timezone = expiration_time.tzinfo
        time_now = datetime.now(timezone)
        if expiration_time < time_now:
            verification["errors"].append("expired")
    print(vc)
    verified = agent.verify_credential(vc)
    verification["checks"].append("proof")
    try:
        if not verified["verified"]:
            verification["errors"].append("invalid proof")
        if len(verification["errors"]) == 0 and len(verification["warnings"]) == 0:
            verification["verified"] = True
        else:
            verification["verified"] = False
        return JSONResponse(status_code=200, content=verified)
    except:
        verification["verified"] = False
        verification["errors"].append("verifier error")
        return JSONResponse(status_code=200, content=verified)


@router.post(
    "/organization/{label}/credentials/status",
    tags=["Credentials"],
    dependencies=[Depends(JWTBearer())],
    summary="Updates the status of an issued credential.",
)
async def update_credential_status(
    label: str, request: Request, request_body: UpdateCredentialStatusSchema
):
    label = is_authorized(label, request)
    request_body = request_body.dict(exclude_none=True)
    credential_id = request_body["credentialId"]
    statuses = request_body["credentialStatus"]

    data_key = f"{label}:credentials:{credential_id}".lower()
    vc = await askar.fetch_data(settings.ASKAR_KEY, data_key)

    did = vc["issuer"] if isinstance(vc["issuer"], str) else vc["issuer"]["id"]
    verkey = agent.get_verkey(did)
    options = {
        "verificationMethod": f"{did}#verkey",
        "proofPurpose": "AssertionMethod",
    }
    for status in statuses:
        status_bit = status["status"]
        status_list_credential = await status_list.change_credential_status(
            vc, status_bit, label
        )
        status_list_vc = agent.sign_json_ld(status_list_credential, options, verkey)
        data_key = f"{label}:status_list"
        await askar.update_data(settings.ASKAR_KEY, data_key, status_list_vc)

    return JSONResponse(status_code=200, content={"message": "Status updated"})


@router.get(
    "/organization/{label}/credentials/status/{list_type}",
    tags=["Credentials"],
    summary="Returns a status list credential",
)
async def get_status_list_credential(label: str, list_type: str):
    label = format_label(label)
    try:
        data_key = f"{label}:status_lists:{list_type}"
        status_list_vc = await askar.fetch_data(settings.ASKAR_KEY, data_key)
    except:
        return ValidationException(
            status_code=404,
            content={"message": f"Status list not found"},
        )
    return status_list_vc
