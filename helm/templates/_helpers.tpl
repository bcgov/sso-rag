{{/*
Expand the name of the chart.
*/}}
{{- define "sso-rag.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
Truncates at 63 chars because some Kubernetes name fields have this limit.
If the release name already contains the chart name it is not repeated.
*/}}
{{- define "sso-rag.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Chart label (name + version).
*/}}
{{- define "sso-rag.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels applied to every resource.
*/}}
{{- define "sso-rag.labels" -}}
helm.sh/chart: {{ include "sso-rag.chart" . }}
{{ include "sso-rag.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels – used in matchLabels and pod template labels.
A component label is added per-resource to disambiguate API vs Ollama pods.
*/}}
{{- define "sso-rag.selectorLabels" -}}
app.kubernetes.io/name: {{ include "sso-rag.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
