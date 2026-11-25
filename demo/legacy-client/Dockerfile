# The unchanged GravePay client (GravePay is FICTIONAL). The same image is used
# before and after; only what answers api.gravepay.demo differs.
# NOT YET BUILT OR RUN: Docker is not installed on the development machine.
FROM python:3.12-slim

WORKDIR /client
COPY gravepay_client.py SHA256 ./
RUN useradd --create-home --uid 10001 client
USER client
# Prove the source is the recorded one, then run it. No base-URL override is set.
CMD ["sh", "-c", "sha256sum -c SHA256 && exec python gravepay_client.py"]
