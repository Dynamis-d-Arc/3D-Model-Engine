#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Character.h"
#include "TwinCharacter.generated.h"

class UCameraComponent;

/** First-person visitor: 1.8 m tall capsule, eye height 1.6 m, walk / fly / ghost (no collision) modes. */
UCLASS()
class TWINWALKTHROUGH_API ATwinCharacter : public ACharacter
{
	GENERATED_BODY()

public:
	ATwinCharacter();

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Twin")
	TObjectPtr<UCameraComponent> Camera;

	void SetFlying(bool bFly);
	void SetGhost(bool bInGhost);
	bool IsFlying() const;
	bool IsGhost() const { return bGhost; }

	static constexpr float EyeHeightCm = 160.f;
	static constexpr float CapsuleHalfHeightCm = 90.f;

private:
	bool bGhost = false;
};
