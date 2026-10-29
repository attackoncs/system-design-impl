# Requirements Document

## Introduction

A Python notification system library implementing the design from *System Design Interview* Chapter 11 "Design A Notification System". The library provides a complete in-process event-driven notification pipeline supporting iOS push (APNs), Android push (FCM), SMS, and Email channels. It features an internal message queue with per-channel queues, a configurable worker pool, notification templates, user preference management (opt-in/opt-out), rate limiting, deduplication, retry with exponential backoff, and event tracking/analytics. The core library has zero runtime dependencies; real third-party provider integrations (SMTP, etc.) are available as optional extras. A lightweight stdlib HTTP demo server exposes REST endpoints for sending notifications, checking delivery status, and viewing analytics.

## Glossary

- **Notification_Service**: The core orchestrator that validates notification requests, queries user preferences, and dispatches notifications to the appropriate message queues
- **Message_Queue**: An in-process async queue that buffers notification tasks, with separate queues per notification channel (iOS, Android, SMS, Email)
- **Worker_Pool**: A pool of async workers that consume tasks from message queues and deliver notifications through third-party providers
- **Worker**: A single async consumer that pulls notification tasks from a queue and invokes the appropriate provider for delivery
- **Provider**: An abstract interface representing a third-party notification delivery service (APNs, FCM, SMS gateway, Email service)
- **Simulated_Provider**: A default provider implementation that simulates delivery with configurable latency and failure rates for testing
- **Notification_Template**: A preformatted notification structure with named placeholders that can be rendered with user-specific parameters
- **Rate_Limiter**: A component that enforces frequency caps on notifications sent to a given user within a configurable time window
- **Deduplicator**: A component that detects and suppresses duplicate notification requests based on content hash and recipient within a configurable time window
- **Notification_Settings**: A per-user, per-channel preference store that tracks opt-in and opt-out status for each notification channel
- **Retry_Handler**: A component that re-enqueues failed notification tasks with exponential backoff up to a configurable maximum retry count
- **Event_Tracker**: A component that records lifecycle events for each notification (created, queued, sent, delivered, failed, clicked, unsubscribed)
- **Notification_Log**: A persistent record of all notification attempts and their outcomes for auditing and analytics
- **Contact_Info**: User contact data including email address, phone number, and device tokens for push notifications
- **Device_Token**: A unique identifier for a mobile device registered to receive push notifications
- **Notification_Request**: A data structure representing a request to send a notification, containing recipient, channel, content, and metadata
- **Notification_Status**: An enumeration of notification lifecycle states (CREATED, QUEUED, SENDING, SENT, DELIVERED, FAILED, CLICKED, UNSUBSCRIBED)

## Requirements

### Requirement 1: Notification Service Orchestration

**User Story:** As a developer, I want a central notification service that validates and dispatches notifications, so that I can send notifications through a single unified interface.

#### Acceptance Criteria

1.1 WHEN a Notification_Request is submitted with a valid recipient and channel, THE Notification_Service SHALL validate the request, check user preferences, and enqueue the notification to the appropriate channel-specific Message_Queue.

1.2 WHEN a Notification_Request is submitted, THE Notification_Service SHALL query the Notification_Settings for the recipient and reject the notification if the user has opted out of the specified channel.

1.3 WHEN a Notification_Request is submitted, THE Notification_Service SHALL pass the request through the Rate_Limiter before enqueueing.

1.4 WHEN a Notification_Request is submitted, THE Notification_Service SHALL pass the request through the Deduplicator before enqueueing.

1.5 IF a Notification_Request has an invalid recipient (missing Contact_Info for the specified channel), THEN THE Notification_Service SHALL raise a ValidationError with a descriptive message.

1.6 IF a Notification_Request specifies a channel for which the recipient has opted out, THEN THE Notification_Service SHALL reject the request and record an Event_Tracker entry with status UNSUBSCRIBED.

1.7 THE Notification_Service SHALL support sending notifications to multiple recipients in a single batch request.

1.8 WHEN a notification is successfully enqueued, THE Notification_Service SHALL return a unique notification identifier for status tracking.

### Requirement 2: Message Queue System

**User Story:** As a developer, I want separate in-process message queues per notification channel, so that each channel can be processed independently with appropriate concurrency.

#### Acceptance Criteria

2.1 THE Message_Queue SHALL maintain separate internal queues for each notification channel: iOS push, Android push, SMS, and Email.

2.2 THE Message_Queue SHALL support async enqueue and dequeue operations using Python asyncio primitives.

2.3 THE Message_Queue SHALL support configurable maximum queue depth per channel.

2.4 IF a queue reaches its maximum depth, THEN THE Message_Queue SHALL reject new enqueue operations and raise a QueueFullError.

2.5 THE Message_Queue SHALL provide methods to query the current depth and pending count for each channel queue.

2.6 THE Message_Queue SHALL preserve FIFO ordering within each channel queue.

### Requirement 3: Worker Pool and Delivery

**User Story:** As a developer, I want a configurable worker pool that processes queued notifications, so that notifications are delivered concurrently with controlled parallelism.

#### Acceptance Criteria

3.1 THE Worker_Pool SHALL spawn a configurable number of Worker instances per notification channel.

3.2 WHEN a Worker pulls a task from the Message_Queue, THE Worker SHALL invoke the configured Provider for that channel to deliver the notification.

3.3 WHEN a Provider returns a successful delivery result, THE Worker SHALL record a SENT event via the Event_Tracker.

3.4 IF a Provider raises a delivery error, THEN THE Worker SHALL pass the failed task to the Retry_Handler for re-enqueue.

3.5 THE Worker_Pool SHALL support graceful shutdown, allowing in-flight deliveries to complete before stopping.

3.6 THE Worker_Pool SHALL support starting and stopping workers dynamically at runtime.

### Requirement 4: Third-Party Provider Abstraction

**User Story:** As a developer, I want a pluggable provider interface, so that I can swap between simulated providers for testing and real integrations for production.

#### Acceptance Criteria

4.1 THE Provider SHALL define an abstract interface with an async method that accepts a notification payload and returns a delivery result.

4.2 THE Simulated_Provider SHALL implement the Provider interface with configurable simulated latency and failure rate.

4.3 THE Provider interface SHALL support four channel types: iOS push (APNs), Android push (FCM), SMS, and Email.

4.4 THE Notification_Service SHALL accept any Provider implementation that conforms to the Provider interface at construction time.

4.5 WHERE real provider integrations are installed as optional extras, THE Notification_Service SHALL support SMTP-based email delivery, APNs HTTP/2 push, FCM HTTP push, and SMS gateway delivery.

4.6 THE Provider SHALL return a structured delivery result containing success status, provider message ID, and optional error details.

### Requirement 5: Notification Templates

**User Story:** As a developer, I want preformatted notification templates with customizable parameters, so that I can maintain consistent messaging while personalizing content per recipient.

#### Acceptance Criteria

5.1 THE Notification_Template SHALL support named placeholders using a defined syntax (e.g., `{variable_name}`).

5.2 WHEN a Notification_Template is rendered with a parameter dictionary, THE Notification_Template SHALL substitute all placeholders with the provided values.

5.3 IF a required placeholder is missing from the parameter dictionary during rendering, THEN THE Notification_Template SHALL raise a TemplateRenderError.

5.4 THE Notification_Template SHALL support separate content fields for title and body.

5.5 THE Notification_Service SHALL accept either a pre-rendered content string or a template identifier with parameters in a Notification_Request.

5.6 THE Notification_Template SHALL support registration and retrieval by a unique template identifier.

### Requirement 6: Rate Limiting

**User Story:** As a developer, I want to enforce frequency caps on notifications per user, so that users are not overwhelmed with excessive messages.

#### Acceptance Criteria

6.1 THE Rate_Limiter SHALL enforce a configurable maximum number of notifications per user within a configurable time window.

6.2 THE Rate_Limiter SHALL support per-channel rate limits (separate limits for iOS, Android, SMS, and Email).

6.3 THE Rate_Limiter SHALL support a global per-user rate limit that applies across all channels.

6.4 IF a notification exceeds the rate limit for the recipient, THEN THE Rate_Limiter SHALL reject the notification and return a RateLimitExceededError.

6.5 WHEN a notification is rejected due to rate limiting, THE Notification_Service SHALL record an Event_Tracker entry indicating the rate limit rejection.

6.6 THE Rate_Limiter SHALL use a sliding window algorithm for time-based frequency tracking.

### Requirement 7: Deduplication

**User Story:** As a developer, I want duplicate notification detection, so that the same notification is not sent to a user multiple times within a short period.

#### Acceptance Criteria

7.1 THE Deduplicator SHALL compute a deduplication key from the combination of recipient identifier, notification channel, and content hash.

7.2 WHEN a notification with a matching deduplication key has been sent within the configurable deduplication window, THE Deduplicator SHALL suppress the duplicate notification.

7.3 THE Deduplicator SHALL support a configurable time-to-live for deduplication entries.

7.4 IF a notification is suppressed as a duplicate, THEN THE Deduplicator SHALL return a DuplicateNotificationError with the original notification identifier.

7.5 THE Deduplicator SHALL automatically expire old deduplication entries to prevent unbounded memory growth.

### Requirement 8: Notification Settings (Opt-in/Opt-out)

**User Story:** As a developer, I want per-user, per-channel notification preferences, so that users can control which types of notifications they receive.

#### Acceptance Criteria

8.1 THE Notification_Settings SHALL store opt-in/opt-out preferences per user and per notification channel.

8.2 THE Notification_Settings SHALL default to opted-in for all channels when a new user is registered.

8.3 WHEN a user opts out of a channel, THE Notification_Settings SHALL persist the preference and immediately prevent further notifications on that channel.

8.4 WHEN a user opts back in to a channel, THE Notification_Settings SHALL restore notification delivery for that channel.

8.5 THE Notification_Settings SHALL provide a method to retrieve all channel preferences for a given user.

8.6 THE Notification_Settings SHALL support bulk preference updates for a user across multiple channels in a single operation.

### Requirement 9: Retry Mechanism

**User Story:** As a developer, I want automatic retry with exponential backoff for failed deliveries, so that transient provider failures do not result in lost notifications.

#### Acceptance Criteria

9.1 WHEN a notification delivery fails, THE Retry_Handler SHALL re-enqueue the notification with an exponentially increasing delay.

9.2 THE Retry_Handler SHALL support a configurable maximum retry count per notification.

9.3 THE Retry_Handler SHALL support a configurable base delay and maximum delay cap for exponential backoff.

9.4 IF the maximum retry count is exceeded, THEN THE Retry_Handler SHALL mark the notification as permanently failed and record a FAILED event via the Event_Tracker.

9.5 THE Retry_Handler SHALL record each retry attempt in the Notification_Log with the attempt number and error details.

9.6 THE Retry_Handler SHALL distinguish between retryable errors (network timeouts, rate limits) and non-retryable errors (invalid token, permanent rejection).

### Requirement 10: Event Tracking and Analytics

**User Story:** As a developer, I want lifecycle event tracking for every notification, so that I can monitor delivery success rates and user engagement.

#### Acceptance Criteria

10.1 THE Event_Tracker SHALL record state transitions for each notification through the lifecycle: CREATED, QUEUED, SENDING, SENT, DELIVERED, FAILED, CLICKED, UNSUBSCRIBED.

10.2 THE Event_Tracker SHALL store each event with a timestamp, notification identifier, channel, and the new status.

10.3 THE Event_Tracker SHALL provide a method to retrieve the full event history for a given notification identifier.

10.4 THE Event_Tracker SHALL provide aggregate analytics: total sent, total delivered, total failed, and total clicked counts within a configurable time range.

10.5 THE Event_Tracker SHALL provide per-channel delivery statistics (success rate, failure rate, average delivery latency).

10.6 THE Notification_Log SHALL persist all notification attempts with their final status for auditing purposes.

### Requirement 11: Contact Information Management

**User Story:** As a developer, I want to manage user contact information and device registrations, so that notifications can be routed to the correct delivery endpoints.

#### Acceptance Criteria

11.1 THE Notification_Service SHALL maintain a Contact_Info store with user email addresses, phone numbers, and Device_Tokens.

11.2 THE Contact_Info store SHALL support multiple Device_Tokens per user for push notification channels.

11.3 WHEN a notification is dispatched, THE Notification_Service SHALL resolve the delivery endpoint from the Contact_Info store based on the recipient and channel.

11.4 THE Contact_Info store SHALL support adding, updating, and removing contact entries and device tokens.

11.5 IF a Device_Token is reported as invalid by a Provider, THEN THE Notification_Service SHALL remove the invalid token from the Contact_Info store.

### Requirement 12: Notification Log and Persistence

**User Story:** As a developer, I want a persistent notification log, so that I can audit delivery history and debug failed notifications.

#### Acceptance Criteria

12.1 THE Notification_Log SHALL record every notification attempt with: notification identifier, recipient, channel, content summary, timestamp, and final status.

12.2 THE Notification_Log SHALL support querying by notification identifier, recipient, channel, status, and time range.

12.3 THE Notification_Log SHALL support configurable retention with automatic cleanup of entries older than the retention period.

12.4 THE Notification_Log SHALL use an in-memory store by default with an abstract interface for pluggable persistent backends.

### Requirement 13: Custom Exceptions

**User Story:** As a developer, I want clear, specific exceptions for all error conditions, so that I can handle notification failures gracefully.

#### Acceptance Criteria

13.1 THE Notification_Service SHALL define a base NotificationError exception from which all custom exceptions inherit.

13.2 THE Notification_Service SHALL define a ValidationError for invalid notification requests.

13.3 THE Notification_Service SHALL define a QueueFullError for queue capacity exceeded conditions.

13.4 THE Notification_Service SHALL define a RateLimitExceededError for rate limit violations.

13.5 THE Notification_Service SHALL define a DuplicateNotificationError for deduplicated notifications.

13.6 THE Notification_Service SHALL define a TemplateRenderError for template rendering failures.

13.7 THE Notification_Service SHALL define a DeliveryError for provider delivery failures.

13.8 THE Notification_Service SHALL include descriptive error messages and relevant context in all custom exceptions.

### Requirement 14: HTTP Demo Server

**User Story:** As a developer, I want a lightweight HTTP demo server, so that I can interact with the notification system through REST endpoints without adding runtime dependencies.

#### Acceptance Criteria

14.1 THE HTTP demo SHALL use only Python standard library modules (http.server, asyncio).

14.2 THE HTTP demo SHALL expose a POST endpoint to send a notification given a recipient, channel, and content.

14.3 THE HTTP demo SHALL expose a GET endpoint to retrieve the delivery status and event history for a notification by its identifier.

14.4 THE HTTP demo SHALL expose a GET endpoint to retrieve aggregate analytics (delivery counts, success rates) per channel.

14.5 THE HTTP demo SHALL expose endpoints to manage user notification settings (get preferences, opt-in, opt-out).

14.6 THE HTTP demo SHALL expose endpoints to manage notification templates (create, list, render preview).

14.7 THE HTTP demo SHALL return appropriate HTTP status codes: 201 for successful notification submission, 200 for queries, 400 for validation errors, 404 for unknown resources, and 429 for rate limit violations.

14.8 THE HTTP demo SHALL use the Notification_Service library as its core logic without duplicating notification processing functionality.

## Non-Functional Requirements

### NFR-1: Project Structure

- **NFR-1.1**: The project SHALL use `pyproject.toml` with hatchling build system.
- **NFR-1.2**: The project SHALL use src layout (`src/notification_system/`).
- **NFR-1.3**: The project SHALL require Python >= 3.9.
- **NFR-1.4**: The core library SHALL have zero runtime dependencies (stdlib only).
- **NFR-1.5**: Real provider integrations SHALL be available as optional extras (e.g., `pip install notification-system[smtp]`).
- **NFR-1.6**: The project SHALL include dev dependencies: pytest and hypothesis.
- **NFR-1.7**: The project SHALL include a `docs/` directory for design documentation.
- **NFR-1.8**: The project SHALL include an `examples/` directory with the HTTP demo server.
- **NFR-1.9**: The project SHALL provide a clean `__init__.py` with public API exports.

### NFR-2: Code Quality

- **NFR-2.1**: The project SHALL include type hints throughout all public interfaces.
- **NFR-2.2**: The project SHALL include unit tests using pytest.
- **NFR-2.3**: The project SHALL include property-based tests using Hypothesis for queue ordering, deduplication correctness, and rate limiting invariants.
- **NFR-2.4**: The project SHALL use abstract base classes (ABC) for all pluggable interfaces (Provider, Storage, etc.).
- **NFR-2.5**: The project SHALL define a custom exceptions module for all error types.
- **NFR-2.6**: The project SHALL use Python asyncio for all internal concurrency (queues, workers, delivery).

### NFR-3: Architecture

- **NFR-3.1**: The system SHALL use an in-process event-driven architecture simulating a distributed notification pipeline within a single process.
- **NFR-3.2**: The internal Message_Queue SHALL use asyncio.Queue for non-blocking async operation.
- **NFR-3.3**: The Worker_Pool SHALL use asyncio tasks for concurrent notification delivery.
- **NFR-3.4**: The system SHALL support configurable concurrency levels per channel (number of workers per queue).

### NFR-4: Performance

- **NFR-4.1**: Notification enqueue operations SHALL complete in under 5 milliseconds for the in-memory backend.
- **NFR-4.2**: The system SHALL support processing at least 1000 notifications per second with the simulated provider.
- **NFR-4.3**: Rate limit checks SHALL complete in under 1 millisecond.
- **NFR-4.4**: Deduplication checks SHALL complete in under 1 millisecond.

### NFR-5: Reliability

- **NFR-5.1**: The system SHALL not lose notifications that have been successfully enqueued, even if a worker fails during delivery.
- **NFR-5.2**: The retry mechanism SHALL use exponential backoff with jitter to prevent thundering herd on provider recovery.
- **NFR-5.3**: The system SHALL support graceful shutdown, completing in-flight deliveries before terminating workers.
