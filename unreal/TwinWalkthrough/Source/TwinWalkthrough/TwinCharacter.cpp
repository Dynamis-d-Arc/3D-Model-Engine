#include "TwinCharacter.h"

#include "Camera/CameraComponent.h"
#include "Components/CapsuleComponent.h"
#include "GameFramework/CharacterMovementComponent.h"

ATwinCharacter::ATwinCharacter()
{
	PrimaryActorTick.bCanEverTick = false;
	GetCapsuleComponent()->InitCapsuleSize(25.f, CapsuleHalfHeightCm);  // 25 cm: a 70 cm clear door must stay navigable

	Camera = CreateDefaultSubobject<UCameraComponent>(TEXT("Camera"));
	Camera->SetupAttachment(GetCapsuleComponent());
	Camera->SetRelativeLocation(FVector(0.f, 0.f, EyeHeightCm - CapsuleHalfHeightCm));
	Camera->bUsePawnControlRotation = true;
	Camera->SetFieldOfView(80.f);
	BaseEyeHeight = EyeHeightCm - CapsuleHalfHeightCm;

	bUseControllerRotationYaw = true;
	bUseControllerRotationPitch = false;
	bUseControllerRotationRoll = false;

	UCharacterMovementComponent* Move = GetCharacterMovement();
	Move->MaxWalkSpeed = 250.f;
	Move->MaxFlySpeed = 450.f;
	Move->BrakingDecelerationWalking = 2000.f;
	Move->BrakingDecelerationFlying = 2500.f;
	Move->JumpZVelocity = 320.f;
	Move->MaxStepHeight = 25.f;
	Move->NavAgentProps.AgentRadius = 25.f;
	Move->NavAgentProps.AgentHeight = 180.f;
	Move->NavAgentProps.bCanWalk = true;
	Move->bUseRVOAvoidance = false;
}

void ATwinCharacter::SetFlying(bool bFly)
{
	GetCharacterMovement()->SetMovementMode(bFly ? MOVE_Flying : MOVE_Walking);
}

void ATwinCharacter::SetGhost(bool bInGhost)
{
	bGhost = bInGhost;
	SetActorEnableCollision(!bGhost);
	if (bGhost)
	{
		SetFlying(true);
	}
}

bool ATwinCharacter::IsFlying() const
{
	return GetCharacterMovement()->MovementMode == MOVE_Flying;
}
