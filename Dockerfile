# Gate-command image for an operator who already has it locally.
# The multi-arch index for python:3.13-slim was resolved on 2026-11-17.
# This image does not install provider CLIs and does not fetch anything at runtime.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

RUN pip install --no-cache-dir "pytest==9.2.0" "bandit==1.9.4"

WORKDIR /workspace
