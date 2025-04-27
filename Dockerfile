FROM svtter/swr-distil:notebook

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

