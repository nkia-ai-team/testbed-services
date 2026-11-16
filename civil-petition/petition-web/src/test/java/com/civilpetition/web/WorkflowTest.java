package com.civilpetition.web;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;

class WorkflowTest {

    @Test
    void acceptMovesReceivedToInProgressAndAnswerCompletesAnyOpenPetition() {
        assertThat(Workflow.next("RECEIVED", "ACCEPT")).isEqualTo("IN_PROGRESS");
        assertThat(Workflow.next("RECEIVED", "ANSWER")).isEqualTo("COMPLETED");
        assertThat(Workflow.next("IN_PROGRESS", "ANSWER")).isEqualTo("COMPLETED");
    }

    @Test
    void closedOrRepeatedStepsAreConflicts() {
        assertThatThrownBy(() -> Workflow.next("COMPLETED", "ANSWER"))
                .isInstanceOfSatisfying(ApiException.class, e -> assertThat(e.status()).isEqualTo(HttpStatus.CONFLICT));
        assertThatThrownBy(() -> Workflow.next("IN_PROGRESS", "ACCEPT"))
                .isInstanceOfSatisfying(ApiException.class, e -> assertThat(e.status()).isEqualTo(HttpStatus.CONFLICT));
        assertThatThrownBy(() -> Workflow.next("RECEIVED", "DELETE"))
                .isInstanceOfSatisfying(ApiException.class, e -> assertThat(e.status()).isEqualTo(HttpStatus.BAD_REQUEST));
    }
}
