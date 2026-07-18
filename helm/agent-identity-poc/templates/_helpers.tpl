{{- define "poc.labels" -}}
app.kubernetes.io/part-of: agent-identity-poc
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}

{{- define "poc.secretName" -}}
{{- if .Values.secrets.create -}}
poc-secrets
{{- else -}}
{{- required "secrets.existingSecret is required when secrets.create=false" .Values.secrets.existingSecret -}}
{{- end -}}
{{- end }}

{{- define "poc.metricsAnnotations" -}}
{{- if .metrics }}
prometheus.io/scrape: "true"
prometheus.io/port: {{ .port | quote }}
prometheus.io/path: "/metrics"
{{- end }}
{{- end }}
