{{- define "platform.storeProvisioner.names" -}}
{{- $serviceAccount := printf "store-provisioner-%s" .Release.Name -}}
{{- if gt (len $serviceAccount) 63 -}}
{{- $serviceAccount = printf "%s-%s" ($serviceAccount | trunc 50 | trimSuffix "-") ($serviceAccount | sha256sum | trunc 12) -}}
{{- end -}}
{{- $scope := printf "%s/%s" .Release.Namespace .Release.Name -}}
{{- $prefix := printf "urumi-store-provisioner-%s-%s-%s" .Release.Namespace .Release.Name ($scope | sha256sum | trunc 12) -}}
{{- dict
    "serviceAccount" $serviceAccount
    "namespaceRole" (printf "%s-namespaces" $prefix)
    "workloadRole" (printf "%s-workloads" $prefix)
    "roleBinding" "urumi-store-provisioner"
    | toJson -}}
{{- end -}}
