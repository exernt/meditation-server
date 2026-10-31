from __future__ import annotations

from pathlib import Path

from aws_cdk import BundlingOptions, CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cloudwatch_actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sns as sns
from aws_cdk import aws_wafv2 as wafv2
from cdk_nag import NagSuppressions
from constructs import Construct

from infra.config import EnvironmentConfig
from infra.stacks.data_stack import DataStack
from infra.stacks.export_stack import ExportStack
from infra.stacks.generation_stack import GenerationStack


class ApiStack(Stack):
    """REST API, FastAPI Lambda boundary, least-privilege policies, and production WAF."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: EnvironmentConfig,
        data_stack: DataStack,
        generation_stack: GenerationStack,
        export_stack: ExportStack,
        alert_topic: sns.ITopic,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        log_removal_policy = (
            RemovalPolicy.RETAIN
            if config.is_production
            else RemovalPolicy.DESTROY
        )

        api_role = iam.Role(
            self,
            "ApiRole",
            role_name=f"{config.prefix}-api-exec",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        api_logs = logs.LogGroup(
            self,
            "ApiLambdaLogs",
            log_group_name=f"/aws/lambda/{config.prefix}-api",
            retention=(logs.RetentionDays.THREE_MONTHS if config.is_production else logs.RetentionDays.TWO_WEEKS),
            removal_policy=log_removal_policy,
        )
        api_logs.grant_write(api_role)

        api_role.add_to_policy(
            iam.PolicyStatement(
                sid="ExecuteAuroraDataApiStatements",
                actions=[
                    "rds-data:BatchExecuteStatement",
                    "rds-data:BeginTransaction",
                    "rds-data:CommitTransaction",
                    "rds-data:ExecuteStatement",
                    "rds-data:RollbackTransaction",
                ],
                resources=[config.database_cluster_arn],
            )
        )
        data_stack.application_secret.grant_read(api_role)
        generation_stack.queue.grant_send_messages(api_role)
        export_stack.queue.grant_send_messages(api_role)
        api_role.add_to_policy(
            iam.PolicyStatement(
                sid="DownloadOwnedResponseExports",
                actions=["s3:GetObject"],
                resources=[export_stack.bucket.arn_for_objects("response-exports/*")],
            )
        )

        google_secret = secretsmanager.Secret.from_secret_complete_arn(
            self, "GoogleMapsSecret", config.google_maps_secret_arn
        )
        google_secret.grant_read(api_role)

        jwt_secret = secretsmanager.Secret(
            self,
            "JwtSigningSecret",
            secret_name=f"{config.prefix}/jwt-signing",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"kid":"initial"}',
                generate_string_key="current",
                password_length=64,
                exclude_punctuation=True,
            ),
        )
        rate_limit_secret = secretsmanager.Secret(
            self,
            "AuthRateLimitSecret",
            secret_name=f"{config.prefix}/auth-rate-limit",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=64,
                exclude_punctuation=True,
            ),
        )
        jwt_secret.grant_read(api_role)
        rate_limit_secret.grant_read(api_role)
        for secret, purpose in (
            (jwt_secret, "JWT keys require coordinated current/previous kid rotation by the application."),
            (rate_limit_secret, "Rotating the HMAC pepper invalidates active throttle buckets and is an operator-run procedure."),
        ):
            NagSuppressions.add_resource_suppressions(
                secret,
                [{"id": "AwsSolutions-SMG4", "reason": purpose}],
                apply_to_children=False,
            )

        api_role.add_to_policy(
            iam.PolicyStatement(
                sid="SendTransactionalEmail",
                actions=["ses:SendEmail"],
                resources=["*"],
                conditions={"StringEquals": {"aws:RequestedRegion": config.region}},
            )
        )
        if config.tts_provider == "polly":
            api_role.add_to_policy(
                iam.PolicyStatement(
                    sid="SynthesizeAustralianEnglishSpeech",
                    actions=["polly:SynthesizeSpeech"],
                    resources=["*"],
                    conditions={"StringEquals": {"aws:RequestedRegion": config.region}},
                )
            )

        project_root = Path(__file__).resolve().parents[2]
        concurrency_options = {}
        if config.api_reserved_concurrency is not None:
            concurrency_options["reserved_concurrent_executions"] = (
                config.api_reserved_concurrency
            )

        self.function = lambda_.Function(
            self,
            "ApiFunction",
            function_name=f"{config.prefix}-api",
            runtime=lambda_.Runtime.PYTHON_3_14,
            handler="meditation_server.api.handler.handler",
            code=lambda_.Code.from_asset(
                str(project_root),
                bundling=BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_14.bundling_image,
                    command=[
                        "bash",
                        "-c",
                        "pip install --no-cache-dir -r /asset-input/requirements-lambda.txt -t /asset-output && cp -r /asset-input/src/. /asset-output/",
                    ],
                ),
            ),
            role=api_role,
            log_group=api_logs,
            logging_format=lambda_.LoggingFormat.JSON,
            application_log_level_v2=lambda_.ApplicationLogLevel.INFO,
            system_log_level_v2=lambda_.SystemLogLevel.INFO,
            timeout=Duration.seconds(30),
            memory_size=1024,
            environment={
                "STAGE": config.stage,
                "DATABASE_RESOURCE_ARN": config.database_cluster_arn,
                "DATABASE_SECRET_ARN": data_stack.application_secret.secret_arn,
                "DATABASE_NAME": config.database_name,
                "GOOGLE_MAPS_SECRET_ARN": google_secret.secret_arn,
                "JWT_SECRET_ARN": jwt_secret.secret_arn,
                "AUTH_RATE_LIMIT_SECRET_ARN": rate_limit_secret.secret_arn,
                "GENERATION_QUEUE_URL": generation_stack.queue.queue_url,
                "RESPONSE_EXPORT_QUEUE_URL": export_stack.queue.queue_url,
                "RESPONSE_EXPORT_BUCKET": export_stack.bucket.bucket_name,
                "RESPONSE_EXPORT_LINK_BASE": config.response_export_link_base,
                "RESPONSE_EXPORT_PRESIGNED_TTL_SECONDS": "300",
                "EMAIL_BACKEND": "ses",
                "PASSWORD_RESET_FROM_EMAIL": config.password_reset_from_email,
                "PASSWORD_RESET_LINK_BASE": config.password_reset_link_base,
                "EMAIL_VERIFICATION_LINK_BASE": config.email_verification_link_base,
                "TTS_PROVIDER": config.tts_provider,
                "TTS_MALE_VOICE_ID": "Russell",
                "TTS_FEMALE_VOICE_ID": "Nicole",
                "TTS_ENGINE": "standard",
                "TTS_LANGUAGE_CODE": "en-AU",
            },
            **concurrency_options,
        )

        access_logs = logs.LogGroup(
            self,
            "ApiGatewayAccessLogs",
            log_group_name=f"/aws/apigateway/{config.prefix}",
            retention=(logs.RetentionDays.THREE_MONTHS if config.is_production else logs.RetentionDays.TWO_WEEKS),
            removal_policy=log_removal_policy,
        )
        self.api = apigateway.RestApi(
            self,
            "RestApi",
            rest_api_name=f"{config.prefix}-api",
            endpoint_types=[apigateway.EndpointType.REGIONAL],
            binary_media_types=["audio/mpeg"],
            # API Gateway exposes one CloudWatch logging role per account and Region.
            # It is configured once as an account prerequisite so dev and prod do
            # not compete to own the singleton AWS::ApiGateway::Account resource.
            cloud_watch_role=False,
            deploy_options=apigateway.StageOptions(
                stage_name=config.stage,
                access_log_destination=apigateway.LogGroupLogDestination(access_logs),
                access_log_format=apigateway.AccessLogFormat.json_with_standard_fields(
                    caller=False,
                    http_method=True,
                    ip=False,
                    protocol=True,
                    request_time=True,
                    resource_path=True,
                    response_length=True,
                    status=True,
                    user=True,
                ),
                logging_level=apigateway.MethodLoggingLevel.OFF,
                metrics_enabled=True,
                tracing_enabled=True,
                throttling_rate_limit=100,
                throttling_burst_limit=50,
            ),
        )
        integration = apigateway.LambdaIntegration(
            self.function,
            proxy=True,
            timeout=Duration.seconds(29),
        )
        self.api.root.add_proxy(default_integration=integration, any_method=True)

        CfnOutput(
            self,
            "ApiBaseUrl",
            value=self.api.url,
            description="Base URL used by clients and post-deployment smoke tests.",
        )

        def notify_on_alarm_and_recovery(alarm: cloudwatch.Alarm) -> None:
            action = cloudwatch_actions.SnsAction(alert_topic)
            alarm.add_alarm_action(action)
            alarm.add_ok_action(action)

        api_metric_dimensions = {
            "ApiName": self.api.rest_api_name,
            "Stage": self.api.deployment_stage.stage_name,
        }
        api_server_error_rate_alarm = cloudwatch.Alarm(
            self,
            "ApiServerErrorRateAlarm",
            alarm_name=f"{config.prefix}-api-5xx-rate",
            alarm_description=(
                "At least 1% of API Gateway requests returned 5xx responses in two "
                "of three five-minute periods. Inspect API access and Lambda logs."
            ),
            metric=cloudwatch.Metric(
                namespace="AWS/ApiGateway",
                metric_name="5XXError",
                dimensions_map=api_metric_dimensions,
                statistic="Average",
                period=Duration.minutes(5),
            ),
            threshold=0.01,
            evaluation_periods=3,
            datapoints_to_alarm=2,
            comparison_operator=(
                cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD
            ),
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        notify_on_alarm_and_recovery(api_server_error_rate_alarm)

        api_server_error_count_alarm = cloudwatch.Alarm(
            self,
            "ApiServerErrorCountAlarm",
            alarm_name=f"{config.prefix}-api-5xx-count",
            alarm_description=(
                "API Gateway returned at least three 5xx responses in five minutes. "
                "Inspect API access and Lambda logs."
            ),
            metric=cloudwatch.Metric(
                namespace="AWS/ApiGateway",
                metric_name="5XXError",
                dimensions_map=api_metric_dimensions,
                statistic="Sum",
                period=Duration.minutes(5),
            ),
            threshold=3,
            evaluation_periods=1,
            comparison_operator=(
                cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD
            ),
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        notify_on_alarm_and_recovery(api_server_error_count_alarm)

        lambda_errors_alarm = cloudwatch.Alarm(
            self,
            "ApiLambdaErrorsAlarm",
            alarm_name=f"{config.prefix}-api-lambda-errors",
            alarm_description=(
                "The API Lambda reported at least one invocation error in five minutes. "
                "Inspect the Lambda logs and affected API request."
            ),
            metric=self.function.metric_errors(
                statistic="Sum",
                period=Duration.minutes(5),
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=(
                cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD
            ),
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        notify_on_alarm_and_recovery(lambda_errors_alarm)

        lambda_duration_alarm = cloudwatch.Alarm(
            self,
            "ApiLambdaDurationAlarm",
            alarm_name=f"{config.prefix}-api-lambda-duration",
            alarm_description=(
                "An API Lambda invocation took at least 25 seconds in two of three "
                "five-minute periods, leaving less than five seconds before timeout."
            ),
            metric=self.function.metric_duration(
                statistic="Maximum",
                period=Duration.minutes(5),
            ),
            threshold=25_000,
            evaluation_periods=3,
            datapoints_to_alarm=2,
            comparison_operator=(
                cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD
            ),
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        notify_on_alarm_and_recovery(lambda_duration_alarm)

        lambda_throttles_alarm = cloudwatch.Alarm(
            self,
            "ApiLambdaThrottlesAlarm",
            alarm_name=f"{config.prefix}-api-lambda-throttles",
            alarm_description=(
                "The API Lambda throttled at least one invocation in five minutes. "
                "Inspect account and reserved concurrency before changing limits."
            ),
            metric=self.function.metric_throttles(
                statistic="Sum",
                period=Duration.minutes(5),
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=(
                cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD
            ),
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        notify_on_alarm_and_recovery(lambda_throttles_alarm)

        NagSuppressions.add_resource_suppressions(
            self.api,
            [
                {
                    "id": "AwsSolutions-APIG2",
                    "reason": "FastAPI/Pydantic validates every request body at the Lambda boundary for the proxy API.",
                },
                {
                    "id": "AwsSolutions-APIG4",
                    "reason": "Public auth routes and custom JWT authorization are enforced in the FastAPI application.",
                },
                {
                    "id": "AwsSolutions-COG4",
                    "reason": "The workload intentionally retains its audited custom authentication flow instead of Cognito.",
                },
            ],
            apply_to_children=True,
        )
        NagSuppressions.add_resource_suppressions(
            api_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "SES SendEmail and Polly SynthesizeSpeech do not support resource-level "
                        "permissions; S3 access is limited to GetObject under the dedicated "
                        "response-exports prefix."
                    ),
                }
            ],
            apply_to_children=True,
        )

        if config.is_production:
            self._attach_production_waf(config, alert_topic)
        else:
            NagSuppressions.add_resource_suppressions(
                self.api.deployment_stage,
                [
                    {
                        "id": "AwsSolutions-APIG3",
                        "reason": "WAF is a mandatory production gate; the synthetic development stage is not public-facing.",
                    },
                    {
                        "id": "AwsSolutions-APIG6",
                        "reason": "Structured access logs and application logs are enabled; API Gateway execution logs are disabled to avoid recording sensitive request content.",
                    }
                ],
                apply_to_children=False,
            )
        if config.is_production:
            NagSuppressions.add_resource_suppressions(
                self.api.deployment_stage,
                [
                    {
                        "id": "AwsSolutions-APIG6",
                        "reason": "Structured access logs and application logs are enabled; API Gateway execution logs are disabled to avoid recording sensitive request content.",
                    }
                ],
                apply_to_children=False,
            )

    def _attach_production_waf(
        self,
        config: EnvironmentConfig,
        alert_topic: sns.ITopic,
    ) -> None:
        count_mode = config.waf_mode == "count"
        rate_rule_action = (
            wafv2.CfnWebACL.RuleActionProperty(count={})
            if count_mode
            else wafv2.CfnWebACL.RuleActionProperty(block={})
        )
        auth_abuse_paths = (
            "/auth/login",
            "/auth/register",
            "/auth/register-researcher",
            "/auth/password-reset/request",
            "/auth/password-reset/confirm",
            "/auth/email-verification/resend",
            "/auth/email-verification/confirm",
        )

        web_acl = wafv2.CfnWebACL(
            self,
            "ProductionWebAcl",
            name=f"{config.prefix}-api",
            scope="REGIONAL",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            association_config=wafv2.CfnWebACL.AssociationConfigProperty(
                request_body={
                    "API_GATEWAY": (
                        wafv2.CfnWebACL.RequestBodyAssociatedResourceTypeConfigProperty(
                            default_size_inspection_limit="KB_64"
                        )
                    )
                }
            ),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name=f"{config.prefix}-api-waf",
                sampled_requests_enabled=False,
            ),
            rules=[
                self._managed_rule(
                    "CommonRuleSet",
                    "AWSManagedRulesCommonRuleSet",
                    10,
                    count_mode=count_mode,
                    count_overrides=("SizeRestrictions_BODY",),
                ),
                self._managed_rule(
                    "KnownBadInputs",
                    "AWSManagedRulesKnownBadInputsRuleSet",
                    20,
                    count_mode=count_mode,
                ),
                self._managed_rule(
                    "IpReputation",
                    "AWSManagedRulesAmazonIpReputationList",
                    30,
                    count_mode=count_mode,
                ),
                wafv2.CfnWebACL.RuleProperty(
                    name="RequestBodySizeLimit",
                    priority=35,
                    action=rate_rule_action,
                    statement=wafv2.CfnWebACL.StatementProperty(
                        size_constraint_statement=(
                            wafv2.CfnWebACL.SizeConstraintStatementProperty(
                                comparison_operator="GT",
                                field_to_match=wafv2.CfnWebACL.FieldToMatchProperty(
                                    body=wafv2.CfnWebACL.BodyProperty(
                                        oversize_handling="MATCH"
                                    )
                                ),
                                size=65_536,
                                text_transformations=[
                                    wafv2.CfnWebACL.TextTransformationProperty(
                                        priority=0,
                                        type="NONE",
                                    )
                                ],
                            )
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name=f"{config.prefix}-body-size",
                        sampled_requests_enabled=False,
                    ),
                ),
                wafv2.CfnWebACL.RuleProperty(
                    name="AuthRateLimit",
                    priority=40,
                    action=rate_rule_action,
                    statement=wafv2.CfnWebACL.StatementProperty(
                        rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                            aggregate_key_type="IP",
                            limit=100,
                            evaluation_window_sec=300,
                            scope_down_statement=wafv2.CfnWebACL.StatementProperty(
                                or_statement=wafv2.CfnWebACL.OrStatementProperty(
                                    statements=[
                                        wafv2.CfnWebACL.StatementProperty(
                                            byte_match_statement=(
                                                wafv2.CfnWebACL.ByteMatchStatementProperty(
                                                    field_to_match=(
                                                        wafv2.CfnWebACL.FieldToMatchProperty(
                                                            uri_path={}
                                                        )
                                                    ),
                                                    positional_constraint="EXACTLY",
                                                    search_string=path,
                                                    text_transformations=[
                                                        wafv2.CfnWebACL.TextTransformationProperty(
                                                            priority=0,
                                                            type="NONE",
                                                        )
                                                    ],
                                                )
                                            )
                                        )
                                        for path in auth_abuse_paths
                                    ]
                                )
                            ),
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name=f"{config.prefix}-auth-rate",
                        sampled_requests_enabled=False,
                    ),
                ),
                wafv2.CfnWebACL.RuleProperty(
                    name="ApiRateLimit",
                    priority=50,
                    action=rate_rule_action,
                    statement=wafv2.CfnWebACL.StatementProperty(
                        rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                            aggregate_key_type="IP",
                            limit=3_000,
                            evaluation_window_sec=300,
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name=f"{config.prefix}-api-rate",
                        sampled_requests_enabled=False,
                    ),
                ),
            ],
        )
        waf_log_group = logs.LogGroup(
            self,
            "WafTrafficLogs",
            log_group_name=f"aws-waf-logs-{config.prefix}-api",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.RETAIN,
        )
        wafv2.CfnLoggingConfiguration(
            self,
            "WafLoggingConfiguration",
            resource_arn=web_acl.attr_arn,
            log_destination_configs=[waf_log_group.log_group_arn],
            redacted_fields=[
                wafv2.CfnLoggingConfiguration.FieldToMatchProperty(
                    single_header={"Name": "authorization"}
                ),
                wafv2.CfnLoggingConfiguration.FieldToMatchProperty(
                    single_header={"Name": "cookie"}
                ),
                wafv2.CfnLoggingConfiguration.FieldToMatchProperty(query_string={}),
            ],
            logging_filter={
                "DefaultBehavior": "DROP",
                "Filters": [
                    {
                        "Behavior": "KEEP",
                        "Requirement": "MEETS_ANY",
                        "Conditions": [
                            {"ActionCondition": {"Action": action}}
                        ],
                    }
                    for action in ("BLOCK", "COUNT")
                ],
            },
        )
        wafv2.CfnWebACLAssociation(
            self,
            "ApiWebAclAssociation",
            resource_arn=(
                f"arn:{self.partition}:apigateway:{self.region}::/restapis/"
                f"{self.api.rest_api_id}/stages/{self.api.deployment_stage.stage_name}"
            ),
            web_acl_arn=web_acl.attr_arn,
        )

        metric_name = "CountedRequests" if count_mode else "BlockedRequests"
        metric_dimensions = {
            "WebACL": f"{config.prefix}-api-waf",
            "Rule": "ALL",
            "Region": config.region,
        }
        waf_matches_alarm = cloudwatch.Alarm(
            self,
            "WafMatchesAlarm",
            alarm_name=f"{config.prefix}-waf-{config.waf_mode}-matches",
            alarm_description=(
                f"AWS WAF recorded at least 25 {config.waf_mode} matches in five "
                "minutes. Inspect the redacted WAF traffic logs before changing rules."
            ),
            metric=cloudwatch.Metric(
                namespace="AWS/WAFV2",
                metric_name=metric_name,
                dimensions_map=metric_dimensions,
                statistic="Sum",
                period=Duration.minutes(5),
            ),
            threshold=25,
            evaluation_periods=1,
            comparison_operator=(
                cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD
            ),
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        waf_matches_alarm.add_alarm_action(cloudwatch_actions.SnsAction(alert_topic))
        waf_matches_alarm.add_ok_action(cloudwatch_actions.SnsAction(alert_topic))

    @staticmethod
    def _managed_rule(
        name: str,
        managed_name: str,
        priority: int,
        *,
        count_mode: bool,
        count_overrides: tuple[str, ...] = (),
    ) -> wafv2.CfnWebACL.RuleProperty:
        return wafv2.CfnWebACL.RuleProperty(
            name=name,
            priority=priority,
            override_action=(
                wafv2.CfnWebACL.OverrideActionProperty(count={})
                if count_mode
                else wafv2.CfnWebACL.OverrideActionProperty(none={})
            ),
            statement=wafv2.CfnWebACL.StatementProperty(
                managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                    vendor_name="AWS",
                    name=managed_name,
                    rule_action_overrides=[
                        wafv2.CfnWebACL.RuleActionOverrideProperty(
                            name=rule_name,
                            action_to_use=wafv2.CfnWebACL.RuleActionProperty(count={}),
                        )
                        for rule_name in count_overrides
                    ],
                )
            ),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name=name,
                sampled_requests_enabled=False,
            ),
        )

