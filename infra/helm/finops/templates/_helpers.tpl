{{- define "finops.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "finops.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "finops.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "finops.labels" -}}
app.kubernetes.io/name: {{ include "finops.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "finops.apiSelector" -}}
app.kubernetes.io/name: {{ include "finops.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: api
{{- end -}}

{{/*
Host headers the API answers to. Defaults to the hosts of whichever front ends are
enabled, which is what the ingress actually sends; a production API refuses to start on a
wildcard, so there is deliberately no "*" fallback.
*/}}
{{- define "finops.trustedHosts" -}}
{{- if .Values.config.trustedHosts -}}
{{- .Values.config.trustedHosts -}}
{{- else -}}
{{- $hosts := list -}}
{{- if .Values.web.execute.enabled -}}{{- $hosts = append $hosts .Values.web.execute.host -}}{{- end -}}
{{- if .Values.web.console.enabled -}}{{- $hosts = append $hosts .Values.web.console.host -}}{{- end -}}
{{- join "," (compact $hosts) -}}
{{- end -}}
{{- end -}}

{{- define "finops.envFrom" -}}
- configMapRef:
    name: {{ include "finops.fullname" . }}-config
- secretRef:
    name: {{ include "finops.fullname" . }}-secrets
{{- end -}}
